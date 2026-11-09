"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_BAUDRATE,
    CONF_IDN,
    CONF_MAX_CURRENT_CLAMP,
    CONF_MAX_VOLTAGE_CLAMP,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .scpi_psu_client import (
    PsuIdentity,
    ScpiProfile,
    ScpiPsuClient,
    TelemetrySnapshot,
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
    status_text: str = "Unresponsive / Power Lost"
    fault_alarm: bool | None = None
    output_enabled: bool | None = None
    voltage_setpoint: float | None = None
    current_setpoint: float | None = None
    voltage_limit: float | None = None
    current_limit: float | None = None
    idn: str | None = None
    system_error: str | None = None
    dmm_reading: float | None = None


class ScpiPsuCoordinator(DataUpdateCoordinator[ScpiPsuData]):
    """Manages serialized SCPI communication via Home Assistant's serialx library."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        command_delay: float = 0.10,
        write_delay: float = 0.15,
    ) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.serial_url: str = cfg[CONF_SERIAL_PORT]
        self.baudrate: int = int(cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
        scan_interval = int(cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

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
        self.parent_device_id: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

        self.client = ScpiPsuClient(
            serial_url=self.serial_url,
            baudrate=self.baudrate,
            initial_idn=initial_idn,
            command_delay=command_delay,
            write_delay=write_delay,
        )
        self._state = ScpiPsuData(idn=initial_idn)
        self._last_full_poll: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def identity(self) -> PsuIdentity:
        return self.client.identity

    @identity.setter
    def identity(self, value: PsuIdentity) -> None:
        self.client.identity = value

    @property
    def profile(self) -> ScpiProfile:
        return self.client.profile

    @property
    def effective_max_voltage(self) -> float:
        hw_max = self.identity.hw_max_voltage
        if self.max_voltage_clamp is not None and self.max_voltage_clamp > 0:
            return min(hw_max, self.max_voltage_clamp)
        return hw_max

    @property
    def effective_max_current(self) -> float:
        hw_max = self.identity.hw_max_current
        if self.max_current_clamp is not None and self.max_current_clamp > 0:
            return min(hw_max, self.max_current_clamp)
        return hw_max

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title or self.identity.default_title,
            manufacturer=self.identity.manufacturer,
            model=self.identity.model,
            serial_number=self.identity.serial_number,
            sw_version=self.identity.firmware,
        )

    def channel_device_info(self, channel: int = 1) -> DeviceInfo | ChildDeviceInfo:
        if self.identity.channels <= 1 or self.parent_device_id is None:
            return self.device_info
        return ChildDeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_ch{channel}")},
            name=f"{self.entry.title or self.identity.default_title} Channel {channel}",
            parent_device_id=self.parent_device_id,
        )

    @property
    def dmm_device_info(self) -> DeviceInfo | ChildDeviceInfo:
        if self.parent_device_id is None:
            return self.device_info
        return ChildDeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_dmm")},
            name=f"{self.entry.title or self.identity.default_title} Multimeter",
            parent_device_id=self.parent_device_id,
        )

    async def async_close(self) -> None:
        await self.client.close()
        self._state.connected = False

    def _mark_unresponsive(self) -> None:
        self._state.responding = False
        self._state.voltage = None
        self._state.current = None
        self._state.power = None
        self._state.status_text = "Unresponsive / Power Lost"
        self._state.fault_alarm = None
        self._state.output_enabled = None

    async def _async_update_data(self) -> ScpiPsuData:
        if not self._state.idn or "," not in self._state.idn:
            ident = await self.client.probe_identity()
            if ident.raw_idn:
                self._state.idn = ident.raw_idn

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
                    await self.client.close()
                    self._consecutive_failures = 0
            self._state.connected = self.client.is_connected
            return self._state

        self._consecutive_failures = 0
        self._state.connected = True
        self._state.responding = True
        self._apply_telemetry(telemetry)

        if telemetry.output_enabled is None:
            outp = await self.client.get_output()
            if outp is not None:
                self._state.output_enabled = outp
                if not self.profile.reports_status_in_telemetry:
                    self._state.status_text = "Active" if outp else "Standby / Output Off"

        now = time.monotonic()
        if now - self._last_full_poll >= 60.0 or self._state.voltage_setpoint is None:
            registers = await self.client.get_config_registers()
            for key in ("voltage_limit", "current_limit", "voltage_setpoint", "current_setpoint"):
                if registers[key] is not None:
                    setattr(self._state, key, registers[key])

            self._state.system_error = await self.client.get_system_error()
            self._last_full_poll = now
        else:
            setpoints = await self.client.get_setpoints()
            if setpoints["voltage_setpoint"] is not None:
                self._state.voltage_setpoint = setpoints["voltage_setpoint"]
            if setpoints["current_setpoint"] is not None:
                self._state.current_setpoint = setpoints["current_setpoint"]

        return self._state

    def _apply_telemetry(self, snap: TelemetrySnapshot) -> None:
        self._state.voltage = snap.voltage
        self._state.current = snap.current
        self._state.power = snap.power
        self._state.ovp_tripped = snap.ovp_tripped
        self._state.ocp_tripped = snap.ocp_tripped
        self._state.opp_tripped = snap.opp_tripped
        self._state.status_text = snap.status_text
        self._state.fault_alarm = snap.fault_alarm
        self._state.dmm_reading = snap.dmm_reading
        if snap.output_enabled is not None:
            self._state.output_enabled = snap.output_enabled

    def _clamp_value(self, value: float, ceiling: float, label: str, unit: str) -> float:
        if value > ceiling:
            _LOGGER.error(
                "SAFETY CLAMP: Requested %s %.3f%s exceeds %.2f%s limit! Clamping to %.2f%s.",
                label,
                value,
                unit,
                ceiling,
                unit,
                ceiling,
                unit,
            )
            value = ceiling
        return max(0.0, value)

    async def async_set_output(self, enabled: bool) -> None:
        actual = await self.client.set_output(enabled)
        snap = await self.client.poll_telemetry()
        if snap is not None:
            self._apply_telemetry(snap)
        self._state.output_enabled = actual
        self.async_set_updated_data(self._state)

    async def async_set_voltage(self, value: float) -> None:
        clamped = self._clamp_value(value, self.effective_max_voltage, "voltage", "V")
        actual = await self.client.set_voltage(clamped)
        self._state.voltage_setpoint = round(actual, 3)
        self.async_set_updated_data(self._state)

    async def async_set_current(self, value: float) -> None:
        clamped = self._clamp_value(value, self.effective_max_current, "current", "A")
        actual = await self.client.set_current(clamped)
        self._state.current_setpoint = round(actual, 3)
        self.async_set_updated_data(self._state)

    async def async_set_voltage_limit(self, value: float) -> None:
        clamped = self._clamp_value(value, self.effective_max_voltage, "OVP limit", "V")
        actual = await self.client.set_voltage_limit(clamped)
        self._state.voltage_limit = round(actual, 3)
        self.async_set_updated_data(self._state)

    async def async_set_current_limit(self, value: float) -> None:
        clamped = self._clamp_value(value, self.effective_max_current, "OCP limit", "A")
        actual = await self.client.set_current_limit(clamped)
        self._state.current_limit = round(actual, 3)
        self.async_set_updated_data(self._state)

    async def async_reset_errors(self) -> None:
        await self.client.clear_faults()
        snap = await self.client.poll_telemetry()
        if snap is not None:
            self._apply_telemetry(snap)
        self._state.system_error = await self.client.get_system_error()
        self.async_set_updated_data(self._state)
