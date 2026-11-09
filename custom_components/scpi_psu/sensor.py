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
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuData, ScpiPsuEntity
from .scpi_psu_client import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
)


@dataclass(frozen=True, kw_only=True)
class ScpiPsuSensorEntityDescription(SensorEntityDescription):
    """Describes SCPI PSU sensor entity."""

    value_fn: Callable[[ScpiPsuData], Any]
    measurement_field: str | None = None
    is_supported_fn: Callable[[ScpiPsuCoordinator], bool] = lambda _: True
    device_info_fn: Callable[[ScpiPsuCoordinator], DeviceInfo | ChildDeviceInfo] = lambda coord: (
        coord.channel_device_info(1)
    )


SENSORS: tuple[ScpiPsuSensorEntityDescription, ...] = (
    ScpiPsuSensorEntityDescription(
        key="voltage",
        name="Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.ch1.voltage,
        measurement_field=FIELD_VOLTAGE,
    ),
    ScpiPsuSensorEntityDescription(
        key="current",
        name="Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.ch1.current,
        measurement_field=FIELD_CURRENT,
    ),
    ScpiPsuSensorEntityDescription(
        key="power",
        name="Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.ch1.power,
        measurement_field=FIELD_POWER,
    ),
    ScpiPsuSensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:power-settings",
        value_fn=lambda d: d.ch1.status_text,
        measurement_field=FIELD_STATUS_TEXT,
    ),
    ScpiPsuSensorEntityDescription(
        key="error_register",
        name="Error Register",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-octagon-outline",
        value_fn=lambda d: d.system_error,
        device_info_fn=lambda coord: coord.device_info,
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
        measurement_field=FIELD_DMM,
        is_supported_fn=lambda coord: coord.identity.has_dmm,
        device_info_fn=lambda coord: coord.dmm_device_info,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU sensors."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ScpiPsuSensor(coordinator, description)
            for description in SENSORS
            if description.is_supported_fn(coordinator)
        ]
    )


class ScpiPsuSensor(ScpiPsuEntity, SensorEntity):
    """Representation of an SCPI PSU sensor."""

    entity_description: ScpiPsuSensorEntityDescription

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = description.device_info_fn(coordinator)
        if description.measurement_field:
            self._tracked_fields = ((description.measurement_field, 1),)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)
