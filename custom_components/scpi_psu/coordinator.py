"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

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
from .scpi_psu_client import (
    PROFILES,
    OwonScpiProfile,
    PsuIdentity,
    ScpiPsuClient,
    StandardScpiProfile,
    parse_idn,
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
        self.serial_url: str = cfg[CONF_SERIAL_PORT]
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

        self.client = ScpiPsuClient(
            serial_url=self.serial_url,
            baudrate=self.baudrate,
            profile_key=self.configured_profile,
            initial_idn=initial_idn,
        )
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
        await self.client.close()
        self._state.connected = False

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

    async def _async_update_data(self) -> ScpiPsuData:
        """Poll the PSU cleanly via ScpiPsuClient."""
        telemetry = await self.client.poll_telemetry()
        if telemetry is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 2:
                if self._state.responding or self._consecutive_failures == 2:
                    _LOGGER.warning(
                        "SCPI PSU unresponsive (%d consecutive failures) — marking sensors unknown",
                        self._consecutive_failures,
                    )
                self._mark_unresponsive()
                if self._consecutive_failures >= 3:
                    await self.client.reconnect()
                    self._consecutive_failures = 0
            self._state.connected = self.client.is_connected
            return self._state

        self._consecutive_failures = 0
        self._state.connected = True
        self._state.responding = True
        self._state.voltage = telemetry.voltage
        self._state.current = telemetry.current
        self._state.power = telemetry.power
        self._state.ovp_tripped = telemetry.ovp_tripped
        self._state.ocp_tripped = telemetry.ocp_tripped
        self._state.opp_tripped = telemetry.opp_tripped
        self._state.status_code = telemetry.status_code
        self._state.status_text = telemetry.status_text
        self._state.fault_alarm = telemetry.fault_alarm
        if telemetry.output_enabled is not None:
            self._state.output_enabled = telemetry.output_enabled

        outp = await self.client.get_output()
        if outp is not None:
            self._state.output_enabled = outp
            if self.profile.key != PROFILE_OWON:
                self._state.status_text = "Active" if outp else "Standby / Output Off"

        now = time.monotonic()
        if now - self._last_full_poll >= 60.0 or self._state.voltage_setpoint is None:
            raw_idn = await self.client.get_raw_idn()
            if raw_idn and "," in raw_idn:
                self._state.idn = raw_idn
                self.identity = parse_idn(raw_idn)
                self.client.identity = self.identity

            registers = await self.client.get_config_registers()
            if registers["voltage_limit"] is not None:
                self._state.voltage_limit = registers["voltage_limit"]
            if registers["current_limit"] is not None:
                self._state.current_limit = registers["current_limit"]
            if registers["voltage_setpoint"] is not None:
                self._state.voltage_setpoint = registers["voltage_setpoint"]
            if registers["current_setpoint"] is not None:
                self._state.current_setpoint = registers["current_setpoint"]

            self._state.system_error = await self.client.get_system_error()
            self._last_full_poll = now

        return self._state

    async def async_set_output(self, enabled: bool) -> None:
        """Turn PSU output ON or OFF."""
        actual = await self.client.set_output(enabled)
        self._state.output_enabled = actual
        snap = await self.client.poll_telemetry()
        if snap is not None:
            self._state.voltage = snap.voltage
            self._state.current = snap.current
            self._state.power = snap.power
            self._state.status_text = snap.status_text
            self._state.fault_alarm = snap.fault_alarm
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
        actual = await self.client.set_voltage(value)
        self._state.voltage_setpoint = round(actual, 3) if actual is not None else round(value, 3)
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
        actual = await self.client.set_current(value)
        self._state.current_setpoint = round(actual, 3) if actual is not None else round(value, 3)
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
        actual = await self.client.set_voltage_limit(value)
        self._state.voltage_limit = round(actual, 3) if actual is not None else round(value, 3)
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
        actual = await self.client.set_current_limit(value)
        self._state.current_limit = round(actual, 3) if actual is not None else round(value, 3)
        self.async_set_updated_data(self._state)

    async def async_reset_errors(self) -> None:
        """Send *CLS to clear protection / error registers."""
        await self.client.clear_faults()
        snap = await self.client.poll_telemetry()
        if snap is not None:
            self._state.voltage = snap.voltage
            self._state.current = snap.current
            self._state.power = snap.power
            self._state.status_text = snap.status_text
            self._state.fault_alarm = snap.fault_alarm
        self._state.system_error = await self.client.get_system_error()
        self.async_set_updated_data(self._state)
