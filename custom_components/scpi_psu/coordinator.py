"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
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
    FIELD_DMM,
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
    voltage_setpoint: float | None = None
    current_setpoint: float | None = None
    voltage_limit: float | None = None
    current_limit: float | None = None
    idn: str | None = None
    system_error: str | None = None
    dmm_reading: float | None = None
    channels: dict[int, ChannelTelemetry] = field(default_factory=dict)
    channel_count: int = 1

    @property
    def ch1(self) -> ChannelTelemetry:
        """Return primary Channel 1 telemetry."""
        return self.channels.get(1) or ChannelTelemetry(channel=1, status_text=STATUS_UNRESPONSIVE)

    @property
    def all_outputs_enabled(self) -> bool | None:
        """Summarize known channel output states."""
        states = [
            self.channels[ch].output_enabled if ch in self.channels else None
            for ch in range(1, self.channel_count + 1)
        ]
        if any(state is True for state in states):
            return True
        return False if all(state is False for state in states) else None

    def apply_telemetry(
        self, snap: TelemetrySnapshot, requested_fields: Collection[str] = ()
    ) -> None:
        channel = self.channels.setdefault(snap.channel, ChannelTelemetry(channel=snap.channel))
        channel.update_snapshot(snap, requested_fields)
        if snap.channel == 1 and (FIELD_DMM in requested_fields or snap.dmm_reading is not None):
            self.dmm_reading = snap.dmm_reading

    def invalidate_outputs(self, channels: Iterable[int]) -> None:
        for channel in channels:
            data = self.channels.setdefault(channel, ChannelTelemetry(channel=channel))
            data.output_enabled = None
            data.status_text = None

    def set_output(self, channel: int, enabled: bool) -> None:
        self.channels.setdefault(
            channel, ChannelTelemetry(channel=channel)
        ).output_enabled = enabled


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
        self._state = ScpiPsuData(idn=initial_idn, channel_count=self.identity.channels)
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
        self._state.dmm_reading = None
        self._state.system_error = None
        self._state.channels = {
            ch: ChannelTelemetry(channel=ch, status_text=STATUS_UNRESPONSIVE)
            for ch in range(1, max(1, self.identity.channels) + 1)
        }

    async def _async_update_data(self) -> ScpiPsuData:
        if not self.identity.is_valid:
            ident = await self.client.probe_identity()
            if ident.raw_idn:
                self._state.idn = ident.raw_idn
                self._state.channel_count = ident.channels
        requested_fields = self.active_fields_by_channel
        all_telemetry = await self.client.poll_all_channels(
            needed_fields_by_channel=requested_fields
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

        for channel, fields in requested_fields.items():
            snap = all_telemetry.get(channel, TelemetrySnapshot(channel=channel))
            self._state.apply_telemetry(snap, fields)
        for channel, snap in all_telemetry.items():
            if channel not in requested_fields:
                self._state.apply_telemetry(snap)

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

        self._state.connected = self.client.is_connected
        return self._state

    def _publish_state(self) -> None:
        self._state.connected = self.client.is_connected
        self.async_set_updated_data(self._state)

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
        try:
            actual = await self.client.set_output(enabled, channel=channel)
            snap = await self.client.poll_telemetry(channel=channel)
        except Exception, asyncio.CancelledError:
            self._state.invalidate_outputs((channel,))
            self._publish_state()
            raise
        self._state.set_output(channel, actual)
        if snap is not None:
            snap.output_enabled = actual
            self._state.apply_telemetry(
                snap, self.client.default_fields_by_channel().get(snap.channel, set())
            )
        self._publish_state()

    async def async_set_all_outputs(self, enabled: bool) -> None:
        """Toggle all channel outputs on a multi-channel device."""
        try:
            actual = await self.client.set_all_outputs(enabled)
            snap = await self.client.poll_telemetry(channel=1)
        except Exception, asyncio.CancelledError:
            self._state.invalidate_outputs(range(1, self.identity.channels + 1))
            self._publish_state()
            raise
        if snap is not None:
            self._state.apply_telemetry(
                snap, self.client.default_fields_by_channel().get(snap.channel, set())
            )
        for ch in range(1, self.identity.channels + 1):
            self._state.set_output(ch, actual)
        self._publish_state()

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
        try:
            actual = await setter(clamped)
        except Exception, asyncio.CancelledError:
            setattr(self._state, state_attr, None)
            self._publish_state()
            raise
        setattr(self._state, state_attr, round(actual, 3))
        self._publish_state()

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
        try:
            await self.client.clear_faults()
            snap = await self.client.poll_telemetry()
            if snap is not None:
                self._state.apply_telemetry(
                    snap, self.client.default_fields_by_channel().get(snap.channel, set())
                )
            self._state.system_error = await self.client.get_system_error()
        except Exception, asyncio.CancelledError:
            self._state.system_error = None
            self._publish_state()
            raise
        self._publish_state()


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
