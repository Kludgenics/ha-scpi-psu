"""Sensor platform for SCPI Power Supply."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuData


@dataclass(frozen=True, kw_only=True)
class ScpiPsuSensorEntityDescription(SensorEntityDescription):
    """Describes SCPI PSU sensor entity."""

    value_fn: Callable[[ScpiPsuData], Any]
    measurement_field: str | None = None


SENSORS: tuple[ScpiPsuSensorEntityDescription, ...] = (
    ScpiPsuSensorEntityDescription(
        key="voltage",
        name="Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.voltage,
        measurement_field="voltage",
    ),
    ScpiPsuSensorEntityDescription(
        key="current",
        name="Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.current,
        measurement_field="current",
    ),
    ScpiPsuSensorEntityDescription(
        key="power",
        name="Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.power,
        measurement_field="power",
    ),
    ScpiPsuSensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:power-settings",
        value_fn=lambda d: d.status_text,
        measurement_field="status_text",
    ),
    ScpiPsuSensorEntityDescription(
        key="error_register",
        name="Error Register",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-octagon-outline",
        value_fn=lambda d: d.system_error,
    ),
    ScpiPsuSensorEntityDescription(
        key="dmm",
        name="Multimeter Reading",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        icon="mdi:multimeter",
        value_fn=lambda d: d.dmm_reading,
        measurement_field="dmm_reading",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU sensors."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ScpiPsuSensor] = []
    for description in SENSORS:
        if description.key == "dmm" and not coordinator.identity.has_dmm:
            continue
        entities.append(ScpiPsuSensor(coordinator, description))
    async_add_entities(entities)


class ScpiPsuSensor(CoordinatorEntity[ScpiPsuCoordinator], SensorEntity):
    """Representation of an SCPI PSU sensor."""

    entity_description: ScpiPsuSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        if description.key == "dmm":
            self._attr_device_info = coordinator.dmm_device_info
        elif description.key in ("voltage", "current", "power", "status"):
            self._attr_device_info = coordinator.channel_device_info(1)
        else:
            self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        """Register field interest with coordinator when entity is added."""
        await super().async_added_to_hass()
        if self.entity_description.measurement_field:
            self.coordinator.register_field_interest(self.entity_description.measurement_field)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister field interest when entity is removed."""
        if self.entity_description.measurement_field:
            self.coordinator.unregister_field_interest(self.entity_description.measurement_field)
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)
