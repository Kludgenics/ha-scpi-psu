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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuData


@dataclass(frozen=True, kw_only=True)
class ScpiPsuNumberEntityDescription(NumberEntityDescription):
    """Describes SCPI PSU number entity."""

    value_fn: Callable[[ScpiPsuData], float | None]
    max_fn: Callable[[ScpiPsuCoordinator], float]
    set_fn: Callable[[ScpiPsuCoordinator, float], Awaitable[None]]


NUMBERS: tuple[ScpiPsuNumberEntityDescription, ...] = (
    ScpiPsuNumberEntityDescription(
        key="voltage_setpoint",
        name="Voltage Setpoint",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=NumberDeviceClass.VOLTAGE,
        native_min_value=0.0,
        native_step=0.01,
        mode=NumberMode.BOX,
        value_fn=lambda d: d.voltage_setpoint,
        max_fn=lambda coord: coord.effective_max_voltage,
        set_fn=lambda coord, val: coord.async_set_voltage(val),
    ),
    ScpiPsuNumberEntityDescription(
        key="current_setpoint",
        name="Current Setpoint",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        native_min_value=0.0,
        native_step=0.01,
        mode=NumberMode.BOX,
        value_fn=lambda d: d.current_setpoint,
        max_fn=lambda coord: coord.effective_max_current,
        set_fn=lambda coord, val: coord.async_set_current(val),
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
        value_fn=lambda d: d.voltage_limit,
        max_fn=lambda coord: coord.effective_max_voltage,
        set_fn=lambda coord, val: coord.async_set_voltage_limit(val),
    ),
    ScpiPsuNumberEntityDescription(
        key="overcurrent_limit",
        name="Overcurrent Limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0.0,
        native_step=0.01,
        mode=NumberMode.BOX,
        value_fn=lambda d: d.current_limit,
        max_fn=lambda coord: coord.effective_max_current,
        set_fn=lambda coord, val: coord.async_set_current_limit(val),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU number entities."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ScpiPsuNumber] = []
    for description in NUMBERS:
        if description.key == "overvoltage_limit" and coordinator.data.voltage_limit is None:
            continue
        if description.key == "overcurrent_limit" and coordinator.data.current_limit is None:
            continue
        entities.append(ScpiPsuNumber(coordinator, description))
    async_add_entities(entities)


class ScpiPsuNumber(CoordinatorEntity[ScpiPsuCoordinator], NumberEntity):
    """Representation of an SCPI PSU number control."""

    entity_description: ScpiPsuNumberEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = coordinator.channel_device_info(1)
        self._attr_native_max_value = description.max_fn(coordinator)

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        await self.entity_description.set_fn(self.coordinator, value)
