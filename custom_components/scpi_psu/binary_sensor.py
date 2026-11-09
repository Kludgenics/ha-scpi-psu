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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuData


@dataclass(frozen=True, kw_only=True)
class ScpiPsuBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes SCPI PSU binary sensor."""

    value_fn: Callable[[ScpiPsuData], bool | None]


BINARY_SENSORS: tuple[ScpiPsuBinarySensorEntityDescription, ...] = (
    ScpiPsuBinarySensorEntityDescription(
        key="fault",
        name="Fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.fault_alarm,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="connected",
        name="Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.connected and d.responding),
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="ovp_tripped",
        name="Overvoltage Protection (OVP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ovp_tripped,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="ocp_tripped",
        name="Overcurrent Protection (OCP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.ocp_tripped,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="opp_tripped",
        name="Overpower Protection (OPP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.opp_tripped,
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
        ScpiPsuBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class ScpiPsuBinarySensor(CoordinatorEntity[ScpiPsuCoordinator], BinarySensorEntity):
    """Representation of an SCPI PSU binary sensor."""

    entity_description: ScpiPsuBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        description: ScpiPsuBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        if description.key in ("fault", "ovp_tripped", "ocp_tripped", "opp_tripped"):
            self._attr_device_info = coordinator.channel_device_info(1)
        else:
            self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return self.entity_description.value_fn(self.coordinator.data)
