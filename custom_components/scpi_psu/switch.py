"""Switch platform for SCPI Power Supply."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU output switch."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScpiPsuOutputSwitch(coordinator)])


class ScpiPsuOutputSwitch(CoordinatorEntity[ScpiPsuCoordinator], SwitchEntity):
    """Representation of the SCPI PSU Output Switch."""

    _attr_has_entity_name = True
    _attr_name = "Output"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:electric-switch"

    def __init__(self, coordinator: ScpiPsuCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_output"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if output is enabled, or None if PSU is unresponsive."""
        return self.coordinator.data.output_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the PSU output on."""
        await self.coordinator.async_set_output(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the PSU output off."""
        await self.coordinator.async_set_output(False)
