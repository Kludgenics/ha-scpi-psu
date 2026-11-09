"""Async SCPI client using serialx transport."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .dialects import PROFILES, ScpiProfile, parse_output_state
from .models import PsuIdentity, TelemetrySnapshot, parse_idn, parse_strict_float

_LOGGER = logging.getLogger(__name__)


class ScpiPsuClient:
    """Serialized async client for SCPI programmable power supplies."""

    def __init__(
        self,
        serial_url: str,
        baudrate: int = 115200,
        initial_idn: str | None = None,
        command_delay: float = 0.10,
        write_delay: float = 0.15,
    ) -> None:
        self.serial_url = serial_url
        self.baudrate = baudrate
        self.identity: PsuIdentity = parse_idn(initial_idn)
        self.command_delay = command_delay
        self.write_delay = write_delay

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    @property
    def profile(self) -> ScpiProfile:
        return PROFILES.get(self.identity.profile_key, PROFILES["standard"])

    async def connect(self) -> None:
        async with self._lock:
            await self._ensure_connected_unlocked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_transport_unlocked()

    async def _close_transport_unlocked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.5)
            except OSError, SerialException, TimeoutError:
                pass
            self._writer = None
            self._reader = None
        self._connected = False

    async def _ensure_connected_unlocked(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            self._connected = True
            return True

        await self._close_transport_unlocked()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                open_serial_connection(
                    url=self.serial_url,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity=Parity.NONE,
                    stopbits=StopBits.ONE,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                ),
                timeout=3.5,
            )
            self._connected = True
            _LOGGER.info("Connected to SCPI PSU at %s", self.serial_url)
            if self.write_delay > 0:
                await asyncio.sleep(self.write_delay)
            self._drain_rx_unlocked()
            return True
        except (OSError, SerialException, TimeoutError) as err:
            _LOGGER.debug("serialx connect to %s failed: %s", self.serial_url, err)
            self._connected = False
            return False

    def _drain_rx_unlocked(self) -> None:
        """Drain any stale unread bytes from the StreamReader buffer non-blockingly."""
        if self._reader is None:
            return
        buffer: bytearray = getattr(self._reader, "_buffer", bytearray())
        if buffer:
            buffer.clear()

    async def query(self, cmd: str, expect_reply: bool = True) -> str | None:
        """Send an SCPI query and return response line."""
        async with self._lock:
            return await self._query_unlocked(cmd, expect_reply=expect_reply)

    async def _query_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        if not await self._ensure_connected_unlocked():
            return None

        self._drain_rx_unlocked()
        payload = (cmd.strip() + "\n").encode("ascii", errors="ignore")

        try:
            assert self._writer is not None
            assert self._reader is not None
            self._writer.write(payload)
            await asyncio.wait_for(self._writer.drain(), timeout=1.5)
            if not expect_reply:
                if self.write_delay > 0:
                    await asyncio.sleep(self.write_delay)
                return ""
            raw_line = await asyncio.wait_for(self._reader.readline(), timeout=1.8)
            if not raw_line:
                await self._close_transport_unlocked()
                return None
            if self.command_delay > 0:
                await asyncio.sleep(self.command_delay)
            return raw_line.decode("utf-8", errors="ignore").strip()

        except TimeoutError:
            _LOGGER.debug("SCPI timeout waiting for reply to '%s'", cmd)
            return None
        except (OSError, SerialException) as err:
            _LOGGER.warning("serialx transport error on '%s': %s", cmd, err)
            await self._close_transport_unlocked()
            return None

    async def probe_identity(self) -> PsuIdentity:
        async with self._lock:
            raw_idn = await self._query_unlocked("*IDN?")
            self.identity = parse_idn(raw_idn)
            return self.identity

    async def select_channel(self, channel: int) -> bool:
        cmd = self.profile.format_select_channel(channel)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            return True

    async def poll_telemetry(self, channel: int = 1) -> TelemetrySnapshot | None:
        async with self._lock:
            prof = self.profile
            if self.identity.channels > 1:
                await self._query_unlocked(prof.format_select_channel(channel), expect_reply=False)

            if prof.cmd_get_composite_telemetry:
                meas_line = await self._query_unlocked(prof.cmd_get_composite_telemetry)
                snap = prof.parse_composite_telemetry(meas_line)
                if snap is not None:
                    snap.channel = channel
                    if self.identity.has_dmm:
                        snap.dmm_reading = parse_strict_float(
                            await self._query_unlocked(prof.cmd_get_dmm_reading)
                        )
                    return snap

            v = parse_strict_float(await self._query_unlocked("MEAS:VOLT?"))
            if v is None or not (0.0 <= v <= 500.0):
                return None
            i = parse_strict_float(await self._query_unlocked("MEAS:CURR?"))
            if i is None or not (0.0 <= i <= 200.0):
                return None
            p = parse_strict_float(await self._query_unlocked("MEAS:POW?"))
            if p is None or not (0.0 <= p <= 10000.0):
                p = round(v * i, 3)

            dmm_val: float | None = None
            if self.identity.has_dmm:
                dmm_val = parse_strict_float(await self._query_unlocked(prof.cmd_get_dmm_reading))

            return TelemetrySnapshot(
                voltage=round(v, 3),
                current=round(i, 3),
                power=round(p, 3),
                dmm_reading=dmm_val,
                channel=channel,
            )

    async def _set_and_verify_float(self, set_cmd: str, get_cmd: str, fallback: float) -> float:
        async with self._lock:
            await self._query_unlocked(set_cmd, expect_reply=False)
            readback = parse_strict_float(await self._query_unlocked(get_cmd))
            return readback if readback is not None else fallback

    async def get_output(self) -> bool | None:
        async with self._lock:
            return parse_output_state(await self._query_unlocked("OUTP?"))

    async def set_output(self, enabled: bool) -> bool:
        cmd = self.profile.format_set_output(enabled)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            parsed = parse_output_state(await self._query_unlocked("OUTP?"))
            return parsed if parsed is not None else enabled

    async def set_voltage(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_voltage(value),
            self.profile.cmd_get_voltage_setpoint,
            value,
        )

    async def set_current(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_current(value),
            self.profile.cmd_get_current_setpoint,
            value,
        )

    async def set_voltage_limit(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_voltage_limit(value),
            self.profile.cmd_get_voltage_limit,
            value,
        )

    async def set_current_limit(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_current_limit(value),
            self.profile.cmd_get_current_limit,
            value,
        )

    async def get_system_error(self) -> str | None:
        async with self._lock:
            err = await self._query_unlocked("SYST:ERR?")
            if err and err not in ("ON", "OFF") and err.count(",") <= 1:
                return err
            return None

    async def _query_bounded_float_unlocked(self, cmd: str, max_val: float) -> float | None:
        val = parse_strict_float(await self._query_unlocked(cmd))
        return round(val, 3) if val is not None and 0.0 <= val <= max_val else None

    async def get_setpoints(self) -> dict[str, float | None]:
        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        async with self._lock:
            return {
                "voltage_setpoint": await self._query_bounded_float_unlocked(
                    prof.cmd_get_voltage_setpoint, max_v_hw
                ),
                "current_setpoint": await self._query_bounded_float_unlocked(
                    prof.cmd_get_current_setpoint, max_i_hw
                ),
            }

    async def get_config_registers(self) -> dict[str, float | None]:
        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        async with self._lock:
            return {
                "voltage_limit": await self._query_bounded_float_unlocked(
                    prof.cmd_get_voltage_limit, max_v_hw
                ),
                "current_limit": await self._query_bounded_float_unlocked(
                    prof.cmd_get_current_limit, max_i_hw
                ),
                "voltage_setpoint": await self._query_bounded_float_unlocked(
                    prof.cmd_get_voltage_setpoint, max_v_hw
                ),
                "current_setpoint": await self._query_bounded_float_unlocked(
                    prof.cmd_get_current_setpoint, max_i_hw
                ),
            }

    async def clear_faults(self) -> None:
        async with self._lock:
            await self._query_unlocked("*CLS", expect_reply=False)
