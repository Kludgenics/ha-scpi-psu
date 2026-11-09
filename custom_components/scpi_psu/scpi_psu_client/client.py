"""Async SCPI client using serialx transport."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .dialects import PROFILES, OwonScpiProfile, StandardScpiProfile
from .models import PsuIdentity, TelemetrySnapshot, parse_idn, parse_strict_float

_LOGGER = logging.getLogger(__name__)


class ScpiPsuClient:
    """Serialized async client for SCPI programmable power supplies."""

    def __init__(
        self,
        serial_url: str,
        baudrate: int = 115200,
        profile_key: str = "auto",
        initial_idn: str | None = None,
    ) -> None:
        """Initialize the SCPI client."""
        self.serial_url = serial_url
        self.baudrate = baudrate
        self.configured_profile = profile_key
        self.identity: PsuIdentity = parse_idn(initial_idn)

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        """Return True if transport connection is established."""
        return self._connected and self._writer is not None and not self._writer.is_closing()

    @property
    def profile(self) -> OwonScpiProfile | StandardScpiProfile:
        """Return the active SCPI profile."""
        if self.configured_profile in PROFILES:
            return PROFILES[self.configured_profile]
        return PROFILES.get(self.identity.profile_key, PROFILES["standard"])

    async def connect(self) -> None:
        """Connect to the serial transport."""
        async with self._lock:
            await self._ensure_connected_unlocked()

    async def close(self) -> None:
        """Close connection cleanly."""
        async with self._lock:
            await self._close_transport_unlocked()

    async def reconnect(self) -> None:
        """Force transport disconnect to allow clean reconnection on next call."""
        async with self._lock:
            await self._close_transport_unlocked()

    async def _close_transport_unlocked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.5)
            except Exception:
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
            await asyncio.sleep(0.15)
            await self._drain_rx_unlocked()
            return True
        except (OSError, SerialException, TimeoutError) as err:
            _LOGGER.debug("serialx connect to %s failed: %s", self.serial_url, err)
            self._connected = False
            return False

    async def _drain_rx_unlocked(self) -> None:
        """Drain any stale unread bytes from the StreamReader before issuing a new query."""
        if self._reader is None:
            return
        try:
            while True:
                chunk = await asyncio.wait_for(self._reader.read(512), timeout=0.02)
                if not chunk:
                    break
        except TimeoutError:
            pass
        except Exception:
            pass

    async def query(self, cmd: str, expect_reply: bool = True) -> str | None:
        """Send an SCPI query and return response line."""
        async with self._lock:
            return await self._query_unlocked(cmd, expect_reply=expect_reply)

    async def _query_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        if not await self._ensure_connected_unlocked():
            return None

        await self._drain_rx_unlocked()
        payload = (cmd.strip() + "\n").encode("ascii", errors="ignore")

        try:
            assert self._writer is not None
            assert self._reader is not None
            self._writer.write(payload)
            await asyncio.wait_for(self._writer.drain(), timeout=1.5)
            if not expect_reply:
                await asyncio.sleep(0.15)
                return ""
            raw_line = await asyncio.wait_for(self._reader.readline(), timeout=1.8)
            if not raw_line:
                await self._close_transport_unlocked()
                return None
            await asyncio.sleep(0.10)
            return raw_line.decode("utf-8", errors="ignore").strip()

        except TimeoutError:
            _LOGGER.debug("SCPI timeout waiting for reply to '%s'", cmd)
            return None
        except (OSError, SerialException) as err:
            _LOGGER.warning("serialx transport error on '%s': %s", cmd, err)
            await self._close_transport_unlocked()
            return None

    async def get_raw_idn(self) -> str | None:
        """Query raw *IDN? string under lock."""
        async with self._lock:
            return await self._query_unlocked("*IDN?")

    async def probe_identity(self) -> PsuIdentity:
        """Probe *IDN? identity under lock."""
        async with self._lock:
            raw_idn = await self._query_unlocked("*IDN?")
            self.identity = parse_idn(raw_idn)
            return self.identity

    async def poll_telemetry(self) -> TelemetrySnapshot | None:
        """Poll telemetry snapshot from the PSU."""
        async with self._lock:
            prof = self.profile
            if prof.key == "owon" and isinstance(prof, OwonScpiProfile):
                meas_line = await self._query_unlocked("MEAS:ALL:INFO?")
                snap = prof.parse_meas_all_info(meas_line)
                if snap is not None:
                    return snap

            # Standard IEEE 488.2 fallback
            v = parse_strict_float(await self._query_unlocked("MEAS:VOLT?"))
            if v is None or not (0.0 <= v <= 500.0):
                return None
            i = parse_strict_float(await self._query_unlocked("MEAS:CURR?"))
            if i is None or not (0.0 <= i <= 200.0):
                return None
            p = parse_strict_float(await self._query_unlocked("MEAS:POW?"))
            if p is None or not (0.0 <= p <= 10000.0):
                p = round(v * i, 3)

            return TelemetrySnapshot(
                voltage=round(v, 3),
                current=round(i, 3),
                power=round(p, 3),
            )

    async def get_output(self) -> bool | None:
        """Read output state (OUTP?)."""
        async with self._lock:
            outp = await self._query_unlocked("OUTP?")
            if outp in ("ON", "1"):
                return True
            if outp in ("OFF", "0"):
                return False
            return None

    async def set_output(self, enabled: bool) -> bool:
        """Set output state (OUTP ON/OFF)."""
        cmd = self.profile.format_set_output(enabled)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            outp = await self._query_unlocked("OUTP?")
            if outp in ("ON", "1"):
                return True
            if outp in ("OFF", "0"):
                return False
            return enabled

    async def set_voltage(self, value: float) -> float | None:
        """Set output voltage setpoint."""
        cmd = self.profile.format_set_voltage(value)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            v_set = parse_strict_float(
                await self._query_unlocked(self.profile.cmd_get_voltage_setpoint)
            )
            return v_set if v_set is not None else value

    async def set_current(self, value: float) -> float | None:
        """Set constant current limit/setpoint."""
        cmd = self.profile.format_set_current(value)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            c_set = parse_strict_float(
                await self._query_unlocked(self.profile.cmd_get_current_setpoint)
            )
            return c_set if c_set is not None else value

    async def set_voltage_limit(self, value: float) -> float | None:
        """Set OVP trip limit."""
        cmd = self.profile.format_set_voltage_limit(value)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            v_lim = parse_strict_float(
                await self._query_unlocked(self.profile.cmd_get_voltage_limit)
            )
            return v_lim if v_lim is not None else value

    async def set_current_limit(self, value: float) -> float | None:
        """Set OCP trip limit."""
        cmd = self.profile.format_set_current_limit(value)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            c_lim = parse_strict_float(
                await self._query_unlocked(self.profile.cmd_get_current_limit)
            )
            return c_lim if c_lim is not None else value

    async def get_system_error(self) -> str | None:
        """Query SYST:ERR? under lock."""
        async with self._lock:
            err = await self._query_unlocked("SYST:ERR?")
            if err and err not in ("ON", "OFF") and err.count(",") <= 1:
                return err
            return None

    async def get_config_registers(self) -> dict[str, float | None]:
        """Query setpoints and limits under lock."""
        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        async with self._lock:
            v_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_limit))
            c_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_limit))
            v_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_setpoint))
            c_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_setpoint))

        return {
            "voltage_limit": round(v_lim, 3)
            if v_lim is not None and 0.0 <= v_lim <= max_v_hw
            else None,
            "current_limit": round(c_lim, 3)
            if c_lim is not None and 0.0 <= c_lim <= max_i_hw
            else None,
            "voltage_setpoint": round(v_set, 3)
            if v_set is not None and 0.0 <= v_set <= max_v_hw
            else None,
            "current_setpoint": round(c_set, 3)
            if c_set is not None and 0.0 <= c_set <= max_i_hw
            else None,
        }

    async def clear_faults(self) -> None:
        """Clear device protection errors (*CLS)."""
        async with self._lock:
            await self._query_unlocked("*CLS", expect_reply=False)
