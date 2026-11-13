"""Button platform for SCPI Power Supply."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SCPI PSU reset button."""
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScpiPsuResetButton(coordinator)])


class ScpiPsuResetButton(ScpiPsuEntity, ButtonEntity):
    """Button to send *CLS (Clear Errors) to SCPI PSU."""

    _attr_name = "Clear Fault"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart-alert"

    def __init__(self, coordinator: ScpiPsuCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_clear_fault"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        await self.coordinator.async_reset_errors()
