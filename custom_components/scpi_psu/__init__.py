"""The SCPI Programmable Power Supply integration."""

from __future__ import annotations

from collections.abc import Sequence

from homeassistant.components.usb import (
    SerialDevice,
    async_register_serial_port_scanner,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import ScpiPsuCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@callback
def _scan_remote_serial_ports(hass: HomeAssistant) -> Sequence[SerialDevice]:
    """Expose configured remote serial URIs (socket://, rfc2217://) to HA's USB/Serial registry."""
    ports: list[SerialDevice] = []
    entries_data: dict[str, ScpiPsuCoordinator] = hass.data.get(DOMAIN, {})
    for coordinator in entries_data.values():
        if "://" in coordinator.serial_url:
            ident = coordinator.identity
            ports.append(
                SerialDevice(
                    device=coordinator.serial_url,
                    serial_number=ident.serial_number,
                    manufacturer=ident.manufacturer,
                    description=f"{ident.model} ({coordinator.entry.title})",
                )
            )
    return ports


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SCPI / SCPI PSU integration."""
    hass.data.setdefault(DOMAIN, {})
    if "usb" in hass.config.components:
        async_register_serial_port_scanner(hass, _scan_remote_serial_ports)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SCPI Power Supply from a config entry."""
    coordinator = ScpiPsuCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()

        device_registry = dr.async_get(hass)
        device_entry = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **coordinator.device_info,
        )
        coordinator.parent_device_id = device_entry.id

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    except BaseException:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await coordinator.async_close()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: ScpiPsuCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
