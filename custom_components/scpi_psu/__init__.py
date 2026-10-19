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
from homeassistant.helpers.typing import ConfigType

from .const import CONF_SERIAL_PORT, DOMAIN
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
    # Migrate legacy 'serial_url' key to standard 'serial_port' key recognized by HA's usb consumer tracker
    if CONF_SERIAL_PORT not in entry.data and "serial_url" in entry.data:
        new_data = {**entry.data, CONF_SERIAL_PORT: entry.data["serial_url"]}
        new_data.pop("serial_url", None)
        new_options = {**entry.options}
        if "serial_url" in new_options:
            new_options[CONF_SERIAL_PORT] = new_options.pop("serial_url")
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options
        )

    coordinator = ScpiPsuCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
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
