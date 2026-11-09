"""Binary sensor platform for SCPI Programmable Power Supply."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuData, ScpiPsuEntity
from .scpi_psu_client import (
    FIELD_FAULT_ALARM,
    FIELD_OCP_TRIPPED,
    FIELD_OPP_TRIPPED,
    FIELD_OVP_TRIPPED,
)


@dataclass(frozen=True, kw_only=True)
class ScpiPsuBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes SCPI PSU binary sensor."""

    value_fn: Callable[[ScpiPsuData], bool | None]
    measurement_field: str | None = None
    is_supported_fn: Callable[[ScpiPsuCoordinator], bool] = lambda coord: (
        coord.profile.reports_status_in_telemetry
    )
    device_info_fn: Callable[[ScpiPsuCoordinator], DeviceInfo | ChildDeviceInfo] = lambda coord: (
        coord.channel_device_info(1)
    )


BINARY_SENSORS: tuple[ScpiPsuBinarySensorEntityDescription, ...] = (
    ScpiPsuBinarySensorEntityDescription(
        key="fault",
        name="Fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.ch1.fault_alarm,
        measurement_field=FIELD_FAULT_ALARM,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="connected",
        name="Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.connected and d.responding),
        is_supported_fn=lambda _: True,
        device_info_fn=lambda coord: coord.device_info,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="ovp_tripped",
        name="Overvoltage Protection (OVP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ch1.ovp_tripped,
        measurement_field=FIELD_OVP_TRIPPED,
        is_supported_fn=lambda coord: coord.profile.supports_ovp,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="ocp_tripped",
        name="Overcurrent Protection (OCP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ch1.ocp_tripped,
        measurement_field=FIELD_OCP_TRIPPED,
        is_supported_fn=lambda coord: coord.profile.supports_ocp,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="opp_tripped",
        name="Overpower Protection (OPP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ch1.opp_tripped,
        measurement_field=FIELD_OPP_TRIPPED,
        is_supported_fn=lambda coord: coord.profile.supports_opp,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU binary sensors."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ScpiPsuBinarySensor(coordinator, description)
            for description in BINARY_SENSORS
            if description.is_supported_fn(coordinator)
        ]
    )


class ScpiPsuBinarySensor(ScpiPsuEntity, BinarySensorEntity):
    """Representation of an SCPI PSU binary sensor."""

    entity_description: ScpiPsuBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = description.device_info_fn(coordinator)
        if description.measurement_field:
            self._tracked_fields = ((description.measurement_field, 1),)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)
