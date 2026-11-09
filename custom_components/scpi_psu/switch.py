"""Switch platform for SCPI Power Supply."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator, ScpiPsuEntity
from .scpi_psu_client import FIELD_OUTPUT_ENABLED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]
    switches: list[SwitchEntity] = []

    if coordinator.identity.channels <= 1:
        switches.append(ScpiPsuOutputSwitch(coordinator))
    else:
        switches.append(ScpiPsuMasterOutputSwitch(coordinator))
        for ch in range(1, coordinator.identity.channels + 1):
            switches.append(ScpiPsuOutputSwitch(coordinator, channel=ch, multi_channel=True))

    async_add_entities(switches)


class ScpiPsuOutputSwitch(ScpiPsuEntity, SwitchEntity):
    _attr_name = "Output"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:electric-switch"

    def __init__(
        self,
        coordinator: ScpiPsuCoordinator,
        channel: int = 1,
        *,
        multi_channel: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self.channel = channel
        prefix = f"_ch{channel}" if multi_channel else ""
        self._attr_unique_id = f"{coordinator.entry.entry_id}{prefix}_output"
        self._attr_device_info = coordinator.channel_device_info(channel)
        self._tracked_fields = ((FIELD_OUTPUT_ENABLED, channel),)

    @property
    def is_on(self) -> bool | None:
        ch_data = self.coordinator.data.channels.get(self.channel)
        return ch_data.output_enabled if ch_data is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_output(True, channel=self.channel)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_output(False, channel=self.channel)


class ScpiPsuMasterOutputSwitch(ScpiPsuEntity, SwitchEntity):
    _attr_name = "All Outputs"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:power-cycle"

    def __init__(self, coordinator: ScpiPsuCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_all_outputs"
        self._attr_device_info = coordinator.device_info
        self._tracked_fields = tuple(
            (FIELD_OUTPUT_ENABLED, ch) for ch in range(1, coordinator.identity.channels + 1)
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.all_outputs_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_all_outputs(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_all_outputs(False)
