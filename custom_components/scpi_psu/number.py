"""Number platform for SCPI Programmable Power Supply."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ScpiDescriptionMixin, ScpiPsuCoordinator, ScpiPsuDescribedEntity


@dataclass(frozen=True, kw_only=True)
class ScpiPsuNumberEntityDescription(ScpiDescriptionMixin[float | None], NumberEntityDescription):
    """Describes SCPI PSU number entity."""

    max_fn: Callable[[ScpiPsuCoordinator], float]
    set_fn: Callable[[ScpiPsuCoordinator, float, int], Awaitable[None]]
    is_channel_supported_fn: Callable[[ScpiPsuCoordinator, int], bool] = lambda _coord, _ch: True


NUMBERS: tuple[ScpiPsuNumberEntityDescription, ...] = (
    ScpiPsuNumberEntityDescription(
        key="voltage_setpoint",
        name="Voltage Setpoint",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=NumberDeviceClass.VOLTAGE,
        native_min_value=0.0,
        native_step=0.01,
        mode=NumberMode.BOX,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.get_channel_config(ch, "voltage_setpoint"),
        max_fn=lambda coord: coord.effective_max_voltage,
        set_fn=lambda coord, val, ch: (
            coord.async_set_voltage(val) if ch == 1 else coord.async_set_voltage(val, channel=ch)
        ),
    ),
    ScpiPsuNumberEntityDescription(
        key="current_setpoint",
        name="Current Setpoint",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        native_min_value=0.0,
        native_step=0.001,
        mode=NumberMode.BOX,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.get_channel_config(ch, "current_setpoint"),
        max_fn=lambda coord: coord.effective_max_current,
        set_fn=lambda coord, val, ch: (
            coord.async_set_current(val) if ch == 1 else coord.async_set_current(val, channel=ch)
        ),
    ),
    ScpiPsuNumberEntityDescription(
        key="overvoltage_limit",
        name="Overvoltage Limit",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=NumberDeviceClass.VOLTAGE,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0.0,
        native_step=0.1,
        mode=NumberMode.BOX,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.get_channel_config(ch, "voltage_limit"),
        max_fn=lambda coord: coord.effective_max_voltage,
        set_fn=lambda coord, val, ch: (
            coord.async_set_voltage_limit(val)
            if ch == 1
            else coord.async_set_voltage_limit(val, channel=ch)
        ),
        is_channel_supported_fn=lambda coord, ch: (
            coord.data.get_channel_config(ch, "voltage_limit") is not None
        ),
    ),
    ScpiPsuNumberEntityDescription(
        key="overcurrent_limit",
        name="Overcurrent Limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0.0,
        native_step=0.001,
        mode=NumberMode.BOX,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.get_channel_config(ch, "current_limit"),
        max_fn=lambda coord: coord.effective_max_current,
        set_fn=lambda coord, val, ch: (
            coord.async_set_current_limit(val)
            if ch == 1
            else coord.async_set_current_limit(val, channel=ch)
        ),
        is_channel_supported_fn=lambda coord, ch: (
            coord.data.get_channel_config(ch, "current_limit") is not None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU number entities."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    channels = range(1, max(1, coordinator.identity.channels) + 1)
    pending: dict[tuple[str, int], ScpiPsuNumberEntityDescription] = {
        (description.key, ch): description for ch in channels for description in NUMBERS
    }
    unsubscribe: Callable[[], None] | None = None

    @callback
    def stop_discovery() -> None:
        nonlocal unsubscribe
        if unsubscribe is not None:
            unsubscribe()
            unsubscribe = None

    @callback
    def add_supported_entities() -> None:
        supported = [
            (key_ch, description)
            for key_ch, description in pending.items()
            if description.is_channel_supported_fn(coordinator, key_ch[1])
        ]
        if not supported:
            return
        for key_ch, _ in supported:
            del pending[key_ch]
        async_add_entities(
            [
                ScpiPsuNumber(coordinator, description, channel=key_ch[1])
                for key_ch, description in supported
            ]
        )
        if not pending:
            stop_discovery()

    add_supported_entities()
    if pending:
        unsubscribe = coordinator.async_add_listener(add_supported_entities)
        entry.async_on_unload(stop_discovery)


class ScpiPsuNumber(ScpiPsuDescribedEntity[ScpiPsuNumberEntityDescription], NumberEntity):
    """Representation of an SCPI PSU number control."""

    entity_description: ScpiPsuNumberEntityDescription

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuNumberEntityDescription,
        channel: int = 1,
    ) -> None:
        super().__init__(coordinator, description, channel=channel)
        self._attr_native_max_value = description.max_fn(coordinator)

    @property
    def native_value(self) -> float | None:
        return self.get_described_value()

    async def async_set_native_value(self, value: float) -> None:
        await self.entity_description.set_fn(self.coordinator, value, self.channel)
