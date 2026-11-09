"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

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
    FAILURES_BEFORE_DISCONNECT,
    FAILURES_BEFORE_UNRESPONSIVE,
    FULL_POLL_INTERVAL,
    PROFILE_AUTO,
    channel_identifier,
    dmm_identifier,
)
from .scpi_psu_client import (
    ChannelTelemetry,
    PsuIdentity,
    ScpiProfile,
    ScpiPsuClient,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)
STATUS_UNRESPONSIVE = "Unresponsive / Power Lost"


@dataclass
class ScpiPsuData:
    """Snapshot of SCPI PSU telemetry and configuration."""

    connected: bool = False
    responding: bool = False
    all_outputs_enabled: bool | None = None
    voltage_setpoint: float | None = None
    current_setpoint: float | None = None
    voltage_limit: float | None = None
    current_limit: float | None = None
    idn: str | None = None
    system_error: str | None = None
    dmm_reading: float | None = None
    channels: dict[int, ChannelTelemetry] = field(default_factory=dict)

    @property
    def ch1(self) -> ChannelTelemetry:
        """Return primary Channel 1 telemetry."""
        return self.channels.setdefault(
            1, ChannelTelemetry(channel=1, status_text=STATUS_UNRESPONSIVE)
        )


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
            profile_key=self.configured_profile
            if self.configured_profile != PROFILE_AUTO
            else None,
            command_delay=command_delay,
            write_delay=write_delay,
        )
        self._state = ScpiPsuData(idn=initial_idn)
        self._registered_fields: dict[int, dict[str, int]] = {}
        self._last_full_poll: float = 0.0
        self._consecutive_failures: int = 0

    def register_field_interest(self, field: str, channel: int = 1) -> None:
        """Register entity interest in a measurement field on a specific channel."""
        ch_map = self._registered_fields.setdefault(channel, {})
        ch_map[field] = ch_map.get(field, 0) + 1

    def unregister_field_interest(self, field: str, channel: int = 1) -> None:
        """Unregister entity interest in a measurement field on a specific channel."""
        ch_map = self._registered_fields.get(channel)
        if ch_map and field in ch_map:
            ch_map[field] -= 1
            if ch_map[field] <= 0:
                del ch_map[field]
            if not ch_map:
                del self._registered_fields[channel]

    @property
    def active_fields_by_channel(self) -> dict[int, set[str]]:
        """Compute the map of channels to actively needed measurement fields."""
        if self._registered_fields:
            return {ch: set(f_map.keys()) for ch, f_map in self._registered_fields.items() if f_map}
        return self.client.default_fields_by_channel()

    @property
    def identity(self) -> PsuIdentity:
        return self.client.identity

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
            identifiers={(DOMAIN, channel_identifier(self.entry.entry_id, channel))},
            name=f"{self.entry.title or self.identity.default_title} Channel {channel}",
            parent_device_id=self.parent_device_id,
        )

    @property
    def dmm_device_info(self) -> DeviceInfo | ChildDeviceInfo:
        if self.parent_device_id is None:
            return self.device_info
        return ChildDeviceInfo(
            identifiers={(DOMAIN, dmm_identifier(self.entry.entry_id))},
            name=f"{self.entry.title or self.identity.default_title} Multimeter",
            parent_device_id=self.parent_device_id,
        )

    async def async_close(self) -> None:
        await self.client.close()
        self._state.connected = False

    def _mark_unresponsive(self) -> None:
        self._state.responding = False
        for ch in range(1, max(1, self.identity.channels) + 1):
            self._state.channels[ch] = ChannelTelemetry(channel=ch, status_text=STATUS_UNRESPONSIVE)

    async def _async_update_data(self) -> ScpiPsuData:
        if not self._state.idn or "," not in self._state.idn:
            ident = await self.client.probe_identity()
            if ident.raw_idn:
                self._state.idn = ident.raw_idn
        all_telemetry = await self.client.poll_all_channels(
            needed_fields_by_channel=self.active_fields_by_channel
        )
        if all_telemetry is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAILURES_BEFORE_UNRESPONSIVE:
                if (
                    self._state.responding
                    or self._consecutive_failures == FAILURES_BEFORE_UNRESPONSIVE
                ):
                    _LOGGER.warning(
                        "SCPI PSU unresponsive (%d consecutive failures) — marking sensors unknown",
                        self._consecutive_failures,
                    )
                self._mark_unresponsive()
                if self._consecutive_failures >= FAILURES_BEFORE_DISCONNECT:
                    await self.client.close()
            self._state.connected = self.client.is_connected
            return self._state

        self._consecutive_failures = 0
        self._state.connected = True
        self._state.responding = True

        for snap in all_telemetry.values():
            self._apply_telemetry(snap)

        now = time.monotonic()
        if now - self._last_full_poll >= FULL_POLL_INTERVAL or self._state.voltage_setpoint is None:
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
        ch = snap.channel
        ch_data = self._state.channels.setdefault(ch, ChannelTelemetry(channel=ch))
        ch_data.apply_snapshot(snap)
        if ch == 1 and snap.dmm_reading is not None:
            self._state.dmm_reading = snap.dmm_reading

        self._sync_all_outputs_enabled()

    def _sync_all_outputs_enabled(self) -> None:
        """Update all_outputs_enabled summary from channel states."""
        if self._state.channels:
            active_states = [
                c.output_enabled
                for c in self._state.channels.values()
                if c.output_enabled is not None
            ]
            self._state.all_outputs_enabled = any(active_states) if active_states else None

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

    async def async_set_output(self, enabled: bool, channel: int = 1) -> None:
        actual = await self.client.set_output(enabled, channel=channel)
        snap = await self.client.poll_telemetry(channel=channel)
        ch_data = self._state.channels.setdefault(channel, ChannelTelemetry(channel=channel))
        ch_data.output_enabled = actual
        if snap is not None:
            snap.output_enabled = actual
            self._apply_telemetry(snap)
        else:
            self._sync_all_outputs_enabled()
        self.async_set_updated_data(self._state)

    async def async_set_all_outputs(self, enabled: bool) -> None:
        """Atomically toggle all channel outputs on a multi-channel device."""
        actual = await self.client.set_all_outputs(enabled)
        snap = await self.client.poll_telemetry(channel=1)
        if snap is not None:
            self._apply_telemetry(snap)
        for ch in range(1, self.identity.channels + 1):
            ch_data = self._state.channels.setdefault(ch, ChannelTelemetry(channel=ch))
            ch_data.output_enabled = actual
        self._state.all_outputs_enabled = actual
        self.async_set_updated_data(self._state)

    async def _async_set_clamped_float(
        self,
        value: float,
        ceiling: float,
        label: str,
        unit: str,
        setter: Callable[[float], Awaitable[float]],
        state_attr: str,
    ) -> None:
        clamped = self._clamp_value(value, ceiling, label, unit)
        actual = await setter(clamped)
        setattr(self._state, state_attr, round(actual, 3))
        self.async_set_updated_data(self._state)

    async def async_set_voltage(self, value: float) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_voltage,
            "voltage",
            "V",
            self.client.set_voltage,
            "voltage_setpoint",
        )

    async def async_set_current(self, value: float) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_current,
            "current",
            "A",
            self.client.set_current,
            "current_setpoint",
        )

    async def async_set_voltage_limit(self, value: float) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_voltage,
            "OVP limit",
            "V",
            self.client.set_voltage_limit,
            "voltage_limit",
        )

    async def async_set_current_limit(self, value: float) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_current,
            "OCP limit",
            "A",
            self.client.set_current_limit,
            "current_limit",
        )

    async def async_reset_errors(self) -> None:
        await self.client.clear_faults()
        snap = await self.client.poll_telemetry()
        if snap is not None:
            self._apply_telemetry(snap)
        self._state.system_error = await self.client.get_system_error()
        self.async_set_updated_data(self._state)


class ScpiPsuEntity(CoordinatorEntity[ScpiPsuCoordinator]):
    """Base entity for SCPI PSU entities managing field-interest registration."""

    _attr_has_entity_name = True
    _tracked_fields: tuple[tuple[str, int], ...] = ()

    async def async_added_to_hass(self) -> None:
        """Register field interest with coordinator when entity is added."""
        await super().async_added_to_hass()
        for field_name, ch in self._tracked_fields:
            self.coordinator.register_field_interest(field_name, channel=ch)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister field interest when entity is removed."""
        for field_name, ch in self._tracked_fields:
            self.coordinator.unregister_field_interest(field_name, channel=ch)
        await super().async_will_remove_from_hass()
