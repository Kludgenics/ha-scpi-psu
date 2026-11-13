"""DataUpdateCoordinator and serialx SCPI client for Programmable Bench Power Supplies."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
    merged_entry_config,
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
        return self.channel_telemetry(1)

    def channel_telemetry(self, channel: int) -> ChannelTelemetry:
        """Return telemetry for a specific channel."""
        return self.channels.get(channel) or ChannelTelemetry(
            channel=channel, status_text=STATUS_UNRESPONSIVE
        )

    def set_channel_config(self, channel: int, key: str, value: float | None) -> None:
        """Store a setpoint or protection limit for a channel."""
        ch_data = self.channels.setdefault(channel, ChannelTelemetry(channel=channel))
        setattr(ch_data, key, value)
        if channel == 1:
            setattr(self, key, value)

    def get_channel_config(self, channel: int, key: str) -> float | None:
        """Read a setpoint or protection limit for a channel."""
        ch_data = self.channels.get(channel)
        if ch_data is not None:
            val = getattr(ch_data, key, None)
            if val is not None:
                return val
        if channel == 1:
            return getattr(self, key, None)
        return None

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
        cfg = merged_entry_config(entry)
        self.serial_url: str = cfg[CONF_SERIAL_PORT]
        baudrate = int(cfg.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
        scan_interval = int(cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        configured_profile: str = cfg.get(CONF_PROFILE, PROFILE_AUTO)

        self.max_voltage_clamp = self._optional_float(cfg.get(CONF_MAX_VOLTAGE_CLAMP))
        self.max_current_clamp = self._optional_float(cfg.get(CONF_MAX_CURRENT_CLAMP))

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
            baudrate=baudrate,
            initial_idn=initial_idn,
            profile_key=configured_profile if configured_profile != PROFILE_AUTO else None,
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

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _effective_limit(hw_max: float, clamp: float | None) -> float:
        if clamp is not None and clamp > 0:
            return min(hw_max, clamp)
        return hw_max

    @property
    def effective_max_voltage(self) -> float:
        return self._effective_limit(self.identity.hw_max_voltage, self.max_voltage_clamp)

    @property
    def effective_max_current(self) -> float:
        return self._effective_limit(self.identity.hw_max_current, self.max_current_clamp)

    @property
    def _device_title(self) -> str:
        return self.entry.title or self.identity.default_title

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self._device_title,
            manufacturer=self.identity.manufacturer,
            model=self.identity.model,
            serial_number=self.identity.serial_number,
            sw_version=self.identity.firmware,
        )

    def _child_device_info(self, identifier: str, suffix: str) -> DeviceInfo | ChildDeviceInfo:
        if self.parent_device_id is None:
            return self.device_info
        return ChildDeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=f"{self._device_title} {suffix}",
            parent_device_id=self.parent_device_id,
        )

    def channel_device_info(self, channel: int = 1) -> DeviceInfo | ChildDeviceInfo:
        if self.identity.channels <= 1:
            return self.device_info
        return self._child_device_info(
            channel_identifier(self.entry.entry_id, channel), f"Channel {channel}"
        )

    @property
    def dmm_device_info(self) -> DeviceInfo | ChildDeviceInfo:
        return self._child_device_info(dmm_identifier(self.entry.entry_id), "Multimeter")

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

        now = time.monotonic()
        full_poll = (
            now - self._last_full_poll >= FULL_POLL_INTERVAL or self._state.voltage_setpoint is None
        )
        if full_poll:
            updates = await self.client.get_config_registers()
            self._state.system_error = await self.client.get_system_error()
            self._last_full_poll = now
        else:
            updates = await self.client.get_setpoints()

        for key, value in updates.items():
            if value is not None:
                self._state.set_channel_config(1, key, value)

        for ch in range(2, self.identity.channels + 1):
            ch_updates = (
                await self.client.get_config_registers(channel=ch)
                if full_poll
                else await self.client.get_setpoints(channel=ch)
            )
            for key, value in ch_updates.items():
                if value is not None:
                    self._state.set_channel_config(ch, key, value)

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

    def _apply_channel_snapshot(self, snap: TelemetrySnapshot | None) -> None:
        if snap is not None:
            self._state.apply_telemetry(
                snap, self.active_fields_by_channel.get(snap.channel, set())
            )

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
            self._apply_channel_snapshot(snap)
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
        self._apply_channel_snapshot(snap)
        for ch in range(1, self.identity.channels + 1):
            self._state.set_output(ch, actual)
        self._publish_state()

    async def _async_set_clamped_float(
        self,
        value: float,
        ceiling: float,
        label: str,
        unit: str,
        setter: Callable[..., Awaitable[float]],
        state_attr: str,
        channel: int = 1,
    ) -> None:
        clamped = self._clamp_value(value, ceiling, label, unit)
        try:
            actual = (
                await setter(clamped) if channel == 1 else await setter(clamped, channel=channel)
            )
        except Exception, asyncio.CancelledError:
            self._state.set_channel_config(channel, state_attr, None)
            self._publish_state()
            raise
        self._state.set_channel_config(channel, state_attr, round(actual, 3))
        self._publish_state()

    async def async_set_voltage(self, value: float, channel: int = 1) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_voltage,
            "voltage",
            "V",
            self.client.set_voltage,
            "voltage_setpoint",
            channel=channel,
        )

    async def async_set_current(self, value: float, channel: int = 1) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_current,
            "current",
            "A",
            self.client.set_current,
            "current_setpoint",
            channel=channel,
        )

    async def async_set_voltage_limit(self, value: float, channel: int = 1) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_voltage,
            "OVP limit",
            "V",
            self.client.set_voltage_limit,
            "voltage_limit",
            channel=channel,
        )

    async def async_set_current_limit(self, value: float, channel: int = 1) -> None:
        await self._async_set_clamped_float(
            value,
            self.effective_max_current,
            "OCP limit",
            "A",
            self.client.set_current_limit,
            "current_limit",
            channel=channel,
        )

    async def async_reset_errors(self) -> None:
        try:
            await self.client.clear_faults()
            snap = await self.client.poll_telemetry()
            self._apply_channel_snapshot(snap)
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


@dataclass(frozen=True, kw_only=True)
class ScpiDescriptionMixin[ValueT](EntityDescription):
    """Shared declarative fields for SCPI PSU entity descriptions."""

    value_fn: Callable[[ScpiPsuData], ValueT] | None = None
    channel_value_fn: Callable[[ScpiPsuData, int], ValueT] | None = None
    per_channel: bool = False
    measurement_field: str | None = None
    is_supported_fn: Callable[[ScpiPsuCoordinator], bool] = lambda _: True
    device_info_fn: Callable[[ScpiPsuCoordinator], DeviceInfo | ChildDeviceInfo] = lambda coord: (
        coord.channel_device_info(1)
    )


class ScpiPsuDescribedEntity[DescT: ScpiDescriptionMixin[Any]](ScpiPsuEntity):
    """Base entity initialized from a declarative description."""

    entity_description: DescT

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: DescT,
        channel: int = 1,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.channel = channel
        if description.per_channel:
            prefix = f"_ch{channel}" if coordinator.identity.channels > 1 else ""
            self._attr_unique_id = f"{coordinator.entry.entry_id}{prefix}_{description.key}"
            self._attr_device_info = coordinator.channel_device_info(channel)
            if description.measurement_field:
                self._tracked_fields = ((description.measurement_field, channel),)
        else:
            self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
            self._attr_device_info = description.device_info_fn(coordinator)
            if description.measurement_field:
                self._tracked_fields = ((description.measurement_field, 1),)

    def get_described_value(self) -> Any:
        """Evaluate the description value function for this entity."""
        if self.entity_description.channel_value_fn is not None:
            return self.entity_description.channel_value_fn(self.coordinator.data, self.channel)
        assert self.entity_description.value_fn is not None
        return self.entity_description.value_fn(self.coordinator.data)


def setup_described_entities[DescT: ScpiDescriptionMixin[Any]](
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    descriptions: Iterable[DescT],
    entity_cls: Callable[..., Entity],
) -> None:
    """Register supported described entities for a config entry."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[Entity] = []
    channels = range(1, max(1, coordinator.identity.channels) + 1)
    for description in descriptions:
        if not description.is_supported_fn(coordinator):
            continue
        if description.per_channel:
            entities.extend(entity_cls(coordinator, description, channel=ch) for ch in channels)
        else:
            entities.append(entity_cls(coordinator, description))
    async_add_entities(entities)
