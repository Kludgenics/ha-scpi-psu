"""Async SCPI client using serialx transport."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .dialects import PROFILES, ScpiProfile, parse_output_state
from .models import PsuIdentity, TelemetrySnapshot, parse_idn, parse_strict_float
from .query_planner import QueryPlanner

_LOGGER = logging.getLogger(__name__)


class ScpiPsuClient:
    """Serialized async client for SCPI programmable power supplies."""

    def __init__(
        self,
        serial_url: str,
        baudrate: int = 115200,
        initial_idn: str | None = None,
        profile_key: str | None = None,
        command_delay: float = 0.10,
        write_delay: float = 0.15,
    ) -> None:
        self.serial_url = serial_url
        self.baudrate = baudrate
        self.identity: PsuIdentity = parse_idn(initial_idn)
        self.configured_profile: str | None = profile_key
        self.command_delay = command_delay
        self.write_delay = write_delay
        self.planner: QueryPlanner = QueryPlanner(
            self.profile, self.identity.channels, self.identity.has_dmm
        )

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    @property
    def profile(self) -> ScpiProfile:
        if self.configured_profile and self.configured_profile in PROFILES:
            return PROFILES[self.configured_profile]
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
            except (OSError, SerialException, TimeoutError) as err:
                _LOGGER.debug("Error while closing transport to %s: %s", self.serial_url, err)
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
        """Drain any stale unread bytes from the transport non-blockingly."""
        if self._reader is None:
            return
        # If StreamReader has an internal buffer with data, clear it without blocking
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
        start_t = time.monotonic()

        try:
            assert self._writer is not None
            assert self._reader is not None
            self._writer.write(payload)
            await asyncio.wait_for(self._writer.drain(), timeout=1.5)
            if not expect_reply:
                if self.write_delay > 0:
                    await asyncio.sleep(self.write_delay)
                elapsed_ms = (time.monotonic() - start_t) * 1000.0
                self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=True)
                return ""
            raw_line = await asyncio.wait_for(self._reader.readline(), timeout=1.8)
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            if not raw_line:
                self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
                await self._close_transport_unlocked()
                return None
            if self.command_delay > 0:
                await asyncio.sleep(self.command_delay)
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=True)
            return raw_line.decode("utf-8", errors="ignore").strip()

        except TimeoutError:
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
            _LOGGER.debug("SCPI timeout waiting for reply to '%s'", cmd)
            return None
        except (OSError, SerialException) as err:
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
            _LOGGER.warning("serialx transport error on '%s': %s", cmd, err)
            await self._close_transport_unlocked()
            return None

    def set_identity(self, identity: PsuIdentity) -> None:
        """Update client identity and reconfigure query planner capabilities."""
        self.identity = identity
        self.planner = QueryPlanner(self.profile, self.identity.channels, self.identity.has_dmm)

    async def probe_identity(self) -> PsuIdentity:
        async with self._lock:
            raw_idn = await self._query_unlocked("*IDN?")
            self.set_identity(parse_idn(raw_idn))
            return self.identity

    async def select_channel(self, channel: int) -> bool:
        cmd = self.profile.format_select_channel(channel)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            return True

    async def poll_telemetry(
        self,
        channel: int = 1,
        needed_fields: set[str] | None = None,
    ) -> TelemetrySnapshot | None:
        async with self._lock:
            prof = self.profile
            if self.identity.channels > 1:
                await self._query_unlocked(prof.format_select_channel(channel), expect_reply=False)

            fields = needed_fields or {
                "voltage",
                "current",
                "power",
                "output_enabled",
                "status_text",
                "fault_alarm",
            }
            if self.identity.has_dmm:
                fields = fields | {"dmm_reading"}

            plan = self.planner.plan_queries(fields, channel=channel)
            if not plan:
                return None

            results: dict[str, Any] = {}
            for desc in plan:
                raw = await self._query_unlocked(desc.command)
                if desc.parser:
                    parsed = desc.parser(raw)
                    results.update(parsed)

            raw_v = results.get("voltage")
            raw_i = results.get("current")
            raw_p = results.get("power")
            if raw_v is None or raw_i is None:
                return None

            val_v = float(raw_v)
            val_i = float(raw_i)
            val_p = float(raw_p) if raw_p is not None else round(val_v * val_i, 3)

            outp = results.get("output_enabled")
            if outp is None:
                outp = await self._get_output_unlocked(channel=channel)

            status = results.get("status_text")
            if status is None or not self.profile.reports_status_in_telemetry:
                if outp is not None:
                    status = "Active" if outp else "Standby / Output Off"
                else:
                    status = "Active"

            return TelemetrySnapshot(
                voltage=val_v,
                current=val_i,
                power=val_p,
                output_enabled=outp,
                status_text=str(status),
                fault_alarm=results.get("fault_alarm"),
                ovp_tripped=results.get("ovp_tripped"),
                ocp_tripped=results.get("ocp_tripped"),
                opp_tripped=results.get("opp_tripped"),
                dmm_reading=results.get("dmm_reading"),
                channel=channel,
            )

    async def _get_output_unlocked(self, channel: int = 1) -> bool | None:
        if self.identity.channels > 1:
            cmd = self.profile.format_get_output(channel)
            raw = await self._query_unlocked(cmd)
            parsed = parse_output_state(raw)
            if parsed is not None:
                return parsed
            if channel > 1:
                await self._query_unlocked(
                    self.profile.format_select_channel(channel), expect_reply=False
                )
        return parse_output_state(await self._query_unlocked("OUTP?"))

    async def poll_all_channels(
        self, needed_fields: set[str] | None = None
    ) -> dict[int, TelemetrySnapshot] | None:
        """Poll telemetry across all configured channels."""
        channels_to_poll = range(1, max(1, self.identity.channels) + 1)
        results: dict[int, TelemetrySnapshot] = {}
        for ch in channels_to_poll:
            snap = await self.poll_telemetry(channel=ch, needed_fields=needed_fields)
            if snap is None:
                # If primary channel fails, instrument is unresponsive
                if ch == 1:
                    return None
                # If channel telemetry query fails, fallback to querying channel output state
                ch_outp = await self.get_output(channel=ch)
                if ch_outp is not None:
                    results[ch] = TelemetrySnapshot(
                        voltage=0.0,
                        current=0.0,
                        power=0.0,
                        output_enabled=ch_outp,
                        channel=ch,
                    )
                continue
            results[ch] = snap
        return results if results else None

    async def _set_and_verify_float(self, set_cmd: str, get_cmd: str, fallback: float) -> float:
        async with self._lock:
            await self._query_unlocked(set_cmd, expect_reply=False)
            readback = parse_strict_float(await self._query_unlocked(get_cmd))
            return readback if readback is not None else fallback

    async def get_output(self, channel: int = 1) -> bool | None:
        async with self._lock:
            return await self._get_output_unlocked(channel=channel)

    async def set_output(self, enabled: bool, channel: int = 1) -> bool:
        async with self._lock:
            if self.identity.channels > 1:
                cmd = self.profile.format_set_output(enabled, channel)
                await self._query_unlocked(cmd, expect_reply=False)
                # Verify readback
                get_cmd = self.profile.format_get_output(channel)
                parsed = parse_output_state(await self._query_unlocked(get_cmd))
                if parsed is not None:
                    return parsed
                await self._query_unlocked(
                    self.profile.format_select_channel(channel), expect_reply=False
                )
            else:
                cmd = self.profile.format_set_output(enabled)
                await self._query_unlocked(cmd, expect_reply=False)

            parsed = parse_output_state(await self._query_unlocked("OUTP?"))
            return parsed if parsed is not None else enabled

    async def set_all_outputs(self, enabled: bool) -> bool:
        """Atomically set all outputs on a multi-channel power supply."""
        async with self._lock:
            # Try atomic broadcast command OUTP:ALL
            cmd = self.profile.format_set_all_outputs(enabled)
            await self._query_unlocked(cmd, expect_reply=False)
            for ch in range(1, self.identity.channels + 1):
                # Ensure each channel state matches
                ch_cmd = self.profile.format_set_output(enabled, ch)
                await self._query_unlocked(ch_cmd, expect_reply=False)
            return enabled

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
