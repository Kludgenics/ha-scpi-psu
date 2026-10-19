"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any

from serialx import Parity, SerialException, StopBits, open_serial_connection

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_BAUDRATE,
    CONF_IDN,
    CONF_MAX_CURRENT_CLAMP,
    CONF_MAX_VOLTAGE_CLAMP,
    CONF_PROFILE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROFILE_AUTO,
    PROFILE_OWON,
)
from .profiles import (
    PROFILES,
    OwonScpiProfile,
    PsuIdentity,
    StandardScpiProfile,
    parse_idn,
    parse_strict_float,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScpiPsuData:
    """Snapshot of SCPI PSU telemetry and configuration."""

    connected: bool = False
    responding: bool = False
    voltage: float | None = None
    current: float | None = None
    power: float | None = None
    ovp_tripped: bool | None = None
    ocp_tripped: bool | None = None
    opp_tripped: bool | None = None
    status_code: int | None = None
    status_text: str = "Unresponsive / Power Lost"
    fault_alarm: bool | None = None
    output_enabled: bool | None = None
    voltage_setpoint: float | None = None
    current_setpoint: float | None = None
    voltage_limit: float | None = None
    current_limit: float | None = None
    idn: str | None = None
    system_error: str | None = None


class ScpiPsuCoordinator(DataUpdateCoordinator[ScpiPsuData]):
    """Manages serialized SCPI communication via Home Assistant's serialx library."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.serial_url: str = cfg.get(CONF_SERIAL_PORT) or cfg["serial_url"]
        self.baudrate: int = int(cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
        scan_interval = int(cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self.configured_profile: str = cfg.get(CONF_PROFILE, PROFILE_AUTO)

        self.max_voltage_clamp: float | None = (
            float(cfg[CONF_MAX_VOLTAGE_CLAMP])
            if cfg.get(CONF_MAX_VOLTAGE_CLAMP) is not None
            else None
        )
        self.max_current_clamp: float | None = (
            float(cfg[CONF_MAX_CURRENT_CLAMP])
            if cfg.get(CONF_MAX_CURRENT_CLAMP) is not None
            else None
        )

        initial_idn = cfg.get(CONF_IDN)
        self.identity: PsuIdentity = parse_idn(initial_idn)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None

        self._state = ScpiPsuData(idn=initial_idn)
        self._last_full_poll: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def profile(self) -> OwonScpiProfile | StandardScpiProfile:
        """Return active SCPI dialect profile."""
        if self.configured_profile in PROFILES:
            return PROFILES[self.configured_profile]
        return PROFILES.get(self.identity.profile_key, PROFILES["standard"])

    @property
    def effective_max_voltage(self) -> float:
        """Return upper voltage ceiling honoring both hardware spec and optional safety clamp."""
        hw_max = self.identity.hw_max_voltage
        if self.max_voltage_clamp is not None and self.max_voltage_clamp > 0:
            return min(hw_max, self.max_voltage_clamp)
        return hw_max

    @property
    def effective_max_current(self) -> float:
        """Return upper current ceiling honoring both hardware spec and optional safety clamp."""
        hw_max = self.identity.hw_max_current
        if self.max_current_clamp is not None and self.max_current_clamp > 0:
            return min(hw_max, self.max_current_clamp)
        return hw_max

    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo for this PSU device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title or self.identity.default_title,
            manufacturer=self.identity.manufacturer,
            model=self.identity.model,
            serial_number=self.identity.serial_number,
            sw_version=self.identity.firmware,
        )

    async def async_close(self) -> None:
        """Close the active serialx transport cleanly."""
        async with self._lock:
            await self._close_transport_unlocked()

    async def _close_transport_unlocked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.5)
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None

        self._state.connected = False

    async def _ensure_connected_unlocked(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            self._state.connected = True
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
            self._state.connected = True
            self._last_full_poll = 0.0
            _LOGGER.info("Connected to SCPI PSU via serialx at %s", self.serial_url)
            await asyncio.sleep(0.15)
            await self._drain_rx_unlocked()
            return True
        except (OSError, SerialException, TimeoutError) as err:
            _LOGGER.debug("serialx connect to %s failed: %s", self.serial_url, err)
            self._state.connected = False
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
        except Exception:  # noqa: BLE001
            pass

    async def _query_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        """Send an SCPI command and optionally read a newline-terminated reply."""
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

    def _mark_unresponsive(self) -> None:
        """Clear live telemetry to None (unavailable/unknown in HA) when PSU stops responding."""
        self._state.responding = False
        self._state.voltage = None
        self._state.current = None
        self._state.power = None
        self._state.status_code = None
        self._state.status_text = "Unresponsive / Power Lost"
        self._state.fault_alarm = None
        self._state.output_enabled = None

    async def _poll_telemetry_unlocked(self) -> bool:
        """Poll live V/I/W and protection status using the active SCPI profile."""
        prof = self.profile

        if prof.key == PROFILE_OWON and isinstance(prof, OwonScpiProfile):
            meas_line = await self._query_unlocked("MEAS:ALL:INFO?")
            snap = prof.parse_meas_all_info(meas_line)
            if snap is not None:
                self._state.responding = True
                self._state.voltage = snap.voltage
                self._state.current = snap.current
                self._state.power = snap.power
                self._state.ovp_tripped = snap.ovp_tripped
                self._state.ocp_tripped = snap.ocp_tripped
                self._state.opp_tripped = snap.opp_tripped
                self._state.status_code = snap.status_code
                self._state.status_text = snap.status_text
                self._state.fault_alarm = snap.fault_alarm
                if snap.output_enabled is not None:
                    self._state.output_enabled = snap.output_enabled
                return True

        # Standard IEEE 488.2 / SCPI fallback (works on all SCPI PSUs including OWON)
        v = parse_strict_float(await self._query_unlocked("MEAS:VOLT?"))
        if v is None or not (0.0 <= v <= 500.0):
            return False
        i = parse_strict_float(await self._query_unlocked("MEAS:CURR?"))
        if i is None or not (0.0 <= i <= 200.0):
            return False
        p = parse_strict_float(await self._query_unlocked("MEAS:POW?"))
        if p is None or not (0.0 <= p <= 10000.0):
            p = round(v * i, 3)

        self._state.responding = True
        self._state.voltage = round(v, 3)
        self._state.current = round(i, 3)
        self._state.power = round(p, 3)
        return True

    async def _async_update_data(self) -> ScpiPsuData:
        """Poll the PSU cleanly under lock."""
        async with self._lock:
            if not await self._poll_telemetry_unlocked():
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    if self._state.responding or self._consecutive_failures == 2:
                        _LOGGER.warning(
                            "SCPI PSU unresponsive (%d consecutive failures) — marking sensors unknown",
                            self._consecutive_failures,
                        )
                    self._mark_unresponsive()
                    if self._consecutive_failures >= 3:
                        await self._close_transport_unlocked()
                        self._consecutive_failures = 0
                return self._state

            self._consecutive_failures = 0

            # Poll output switch state every cycle
            outp_line = await self._query_unlocked("OUTP?")
            if outp_line in ("ON", "1"):
                self._state.output_enabled = True
                if self.profile.key != PROFILE_OWON:
                    self._state.status_text = "Active"
            elif outp_line in ("OFF", "0"):
                self._state.output_enabled = False
                if self.profile.key != PROFILE_OWON:
                    self._state.status_text = "Standby / Output Off"

            # Poll setpoints, limits, IDN, and error register on first connect and every 60s
            now = time.monotonic()
            if now - self._last_full_poll >= 60.0 or self._state.voltage_setpoint is None:
                await self._poll_config_registers_unlocked()
                self._last_full_poll = now

            return self._state

    async def _poll_config_registers_unlocked(self) -> None:
        """Poll setpoints, limits, IDN, and system error register with strict validation."""
        idn = await self._query_unlocked("*IDN?")
        if idn and "," in idn:
            self._state.idn = idn
            self.identity = parse_idn(idn)

        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        v_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_limit))
        if v_lim is not None and 0.0 <= v_lim <= max_v_hw:
            self._state.voltage_limit = round(v_lim, 3)

        c_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_limit))
        if c_lim is not None and 0.0 <= c_lim <= max_i_hw:
            self._state.current_limit = round(c_lim, 3)

        v_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_setpoint))
        if v_set is not None and 0.0 <= v_set <= max_v_hw:
            self._state.voltage_setpoint = round(v_set, 3)

        c_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_setpoint))
        if c_set is not None and 0.0 <= c_set <= max_i_hw:
            self._state.current_setpoint = round(c_set, 3)

        err = await self._query_unlocked("SYST:ERR?")
        if err and err not in ("ON", "OFF") and err.count(",") <= 1:
            self._state.system_error = err

    # --- Control Actions ---

    async def async_set_output(self, enabled: bool) -> None:
        """Turn PSU output ON or OFF."""
        cmd = self.profile.format_set_output(enabled)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            outp = await self._query_unlocked("OUTP?")
            if outp in ("ON", "1"):
                self._state.output_enabled = True
            elif outp in ("OFF", "0"):
                self._state.output_enabled = False
            else:
                self._state.output_enabled = enabled
            await self._poll_telemetry_unlocked()
        self.async_set_updated_data(self._state)

    async def async_set_voltage(self, value: float) -> None:
        """Set output voltage (clamped to effective_max_voltage)."""
        max_v = self.effective_max_voltage
        if value > max_v:
            _LOGGER.error(
                "SAFETY CLAMP: Requested voltage %.3fV exceeds %.1fV limit! Clamping to %.1fV.",
                value,
                max_v,
                max_v,
            )
            value = max_v
        value = max(0.0, value)
        prof = self.profile
        async with self._lock:
            await self._query_unlocked(prof.format_set_voltage(value), expect_reply=False)
            v_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_setpoint))
            if v_set is not None and 0.0 <= v_set <= self.identity.hw_max_voltage * 1.15:
                self._state.voltage_setpoint = round(v_set, 3)
            else:
                self._state.voltage_setpoint = round(value, 3)
        self.async_set_updated_data(self._state)

    async def async_set_current(self, value: float) -> None:
        """Set output current limit/setpoint (clamped to effective_max_current)."""
        max_i = self.effective_max_current
        if value > max_i:
            _LOGGER.error(
                "SAFETY CLAMP: Requested current %.3fA exceeds %.2fA limit! Clamping to %.2fA.",
                value,
                max_i,
                max_i,
            )
            value = max_i
        value = max(0.0, value)
        prof = self.profile
        async with self._lock:
            await self._query_unlocked(prof.format_set_current(value), expect_reply=False)
            c_set = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_setpoint))
            if c_set is not None and 0.0 <= c_set <= self.identity.hw_max_current * 1.15:
                self._state.current_setpoint = round(c_set, 3)
            else:
                self._state.current_setpoint = round(value, 3)
        self.async_set_updated_data(self._state)

    async def async_set_voltage_limit(self, value: float) -> None:
        """Set OVP voltage protection limit (clamped to effective_max_voltage)."""
        max_v = self.effective_max_voltage
        if value > max_v:
            _LOGGER.error(
                "SAFETY CLAMP: Requested OVP limit %.3fV exceeds %.1fV limit! Clamping to %.1fV.",
                value,
                max_v,
                max_v,
            )
            value = max_v
        value = max(0.0, value)
        prof = self.profile
        async with self._lock:
            await self._query_unlocked(prof.format_set_voltage_limit(value), expect_reply=False)
            v_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_voltage_limit))
            if v_lim is not None and 0.0 <= v_lim <= self.identity.hw_max_voltage * 1.15:
                self._state.voltage_limit = round(v_lim, 3)
            else:
                self._state.voltage_limit = round(value, 3)
        self.async_set_updated_data(self._state)

    async def async_set_current_limit(self, value: float) -> None:
        """Set OCP current protection limit (clamped to effective_max_current)."""
        max_i = self.effective_max_current
        if value > max_i:
            _LOGGER.error(
                "SAFETY CLAMP: Requested OCP limit %.3fA exceeds %.2fA limit! Clamping to %.2fA.",
                value,
                max_i,
                max_i,
            )
            value = max_i
        value = max(0.0, value)
        prof = self.profile
        async with self._lock:
            await self._query_unlocked(prof.format_set_current_limit(value), expect_reply=False)
            c_lim = parse_strict_float(await self._query_unlocked(prof.cmd_get_current_limit))
            if c_lim is not None and 0.0 <= c_lim <= self.identity.hw_max_current * 1.15:
                self._state.current_limit = round(c_lim, 3)
            else:
                self._state.current_limit = round(value, 3)
        self.async_set_updated_data(self._state)

    async def async_reset_errors(self) -> None:
        """Send *CLS to clear protection / error registers."""
        async with self._lock:
            await self._query_unlocked("*CLS", expect_reply=False)
            await self._poll_telemetry_unlocked()
            err = await self._query_unlocked("SYST:ERR?")
            if err and err not in ("ON", "OFF") and err.count(",") <= 1:
                self._state.system_error = err
        self.async_set_updated_data(self._state)
