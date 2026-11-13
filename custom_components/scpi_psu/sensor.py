"""Sensor platform for SCPI Power Supply."""

from __future__ import annotations

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

from .coordinator import (
    ScpiDescriptionMixin,
    ScpiPsuDescribedEntity,
    setup_described_entities,
)
from .scpi_psu_client import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
)


@dataclass(frozen=True, kw_only=True)
class ScpiPsuSensorEntityDescription(ScpiDescriptionMixin[Any], SensorEntityDescription):
    """Describes SCPI PSU sensor entity."""


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
    setup_described_entities(hass, entry, async_add_entities, SENSORS, ScpiPsuSensor)


class ScpiPsuSensor(ScpiPsuDescribedEntity[ScpiPsuSensorEntityDescription], SensorEntity):
    """Representation of an SCPI PSU sensor."""

    entity_description: ScpiPsuSensorEntityDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)
