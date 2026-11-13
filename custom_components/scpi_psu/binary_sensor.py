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

from .coordinator import (
    ScpiDescriptionMixin,
    ScpiPsuCoordinator,
    ScpiPsuDescribedEntity,
    setup_described_entities,
)
from .scpi_psu_client import (
    FIELD_FAULT_ALARM,
    FIELD_OCP_TRIPPED,
    FIELD_OPP_TRIPPED,
    FIELD_OVP_TRIPPED,
)


@dataclass(frozen=True, kw_only=True)
class ScpiPsuBinarySensorEntityDescription(
    ScpiDescriptionMixin[bool | None], BinarySensorEntityDescription
):
    """Describes SCPI PSU binary sensor."""

    is_supported_fn: Callable[[ScpiPsuCoordinator], bool] = lambda coord: (
        coord.profile.reports_status_in_telemetry
    )


BINARY_SENSORS: tuple[ScpiPsuBinarySensorEntityDescription, ...] = (
    ScpiPsuBinarySensorEntityDescription(
        key="fault",
        name="Fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.channel_telemetry(ch).fault_alarm,
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
        per_channel=True,
        channel_value_fn=lambda d, ch: d.channel_telemetry(ch).ovp_tripped,
        measurement_field=FIELD_OVP_TRIPPED,
        is_supported_fn=lambda coord: coord.profile.supports_ovp,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="ocp_tripped",
        name="Overcurrent Protection (OCP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.channel_telemetry(ch).ocp_tripped,
        measurement_field=FIELD_OCP_TRIPPED,
        is_supported_fn=lambda coord: coord.profile.supports_ocp,
    ),
    ScpiPsuBinarySensorEntityDescription(
        key="opp_tripped",
        name="Over-temperature Protection (OTP)",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        per_channel=True,
        channel_value_fn=lambda d, ch: d.channel_telemetry(ch).opp_tripped,
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
    setup_described_entities(hass, entry, async_add_entities, BINARY_SENSORS, ScpiPsuBinarySensor)


class ScpiPsuBinarySensor(
    ScpiPsuDescribedEntity[ScpiPsuBinarySensorEntityDescription], BinarySensorEntity
):
    """Representation of an SCPI PSU binary sensor."""

    entity_description: ScpiPsuBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        return self.get_described_value()
