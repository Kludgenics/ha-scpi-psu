"""Tests for SCPI PSU platforms (sensor, binary_sensor, switch, number, button)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu import (
    binary_sensor,
    button,
    number,
    sensor,
    switch,
)
from custom_components.scpi_psu.binary_sensor import BINARY_SENSORS
from custom_components.scpi_psu.const import CONF_SERIAL_PORT, DOMAIN
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator, ScpiPsuData
from custom_components.scpi_psu.number import NUMBERS


async def test_sensor_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_sensor_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        voltage=12.34,
        current=1.5,
        power=18.51,
        status_text="Active",
        system_error="0,No error",
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[sensor.ScpiPsuSensor] = []
    await sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    # Default device has no DMM, so all sensors except dmm are registered
    assert len(added) == 5

    sensor_map = {ent.entity_description.key: ent for ent in added}
    assert sensor_map["voltage"].available is True
    assert sensor_map["voltage"].native_value == 12.34
    assert sensor_map["current"].native_value == 1.5
    assert sensor_map["power"].native_value == 18.51
    assert sensor_map["status"].native_value == "Active"
    assert sensor_map["error_register"].native_value == "0,No error"

    # Test when device has DMM capability (e.g. SPM6103)
    from custom_components.scpi_psu.scpi_psu_client.models import PsuIdentity

    coordinator.identity = PsuIdentity(
        manufacturer="OWON",
        model="SPM6103",
        serial_number="123",
        firmware="1.0",
        raw_idn="OWON,SPM6103,123,1.0",
        profile_key="owon",
        hw_max_voltage=60.0,
        hw_max_current=10.0,
        has_dmm=True,
    )
    coordinator.data.dmm_reading = 3.3
    dmm_added: list[sensor.ScpiPsuSensor] = []
    await sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: dmm_added.extend(ents))
    )
    assert len(dmm_added) == 6
    dmm_map = {ent.entity_description.key: ent for ent in dmm_added}
    assert dmm_map["dmm"].native_value == 3.3


async def test_binary_sensor_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_bin_sensor_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        connected=True,
        responding=True,
        fault_alarm=False,
        ovp_tripped=False,
        ocp_tripped=True,
        opp_tripped=False,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[binary_sensor.ScpiPsuBinarySensor] = []
    await binary_sensor.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == len(BINARY_SENSORS)

    bin_map = {ent.entity_description.key: ent for ent in added}
    assert bin_map["connected"].is_on is True
    assert bin_map["fault"].is_on is False
    assert bin_map["ocp_tripped"].is_on is True


async def test_switch_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_switch_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(output_enabled=True)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[switch.ScpiPsuOutputSwitch] = []
    await switch.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 1

    sw = added[0]
    assert sw.is_on is True

    with patch.object(coordinator, "async_set_output", new_callable=AsyncMock) as mock_set:
        await sw.async_turn_off()
        mock_set.assert_awaited_once_with(False)

        await sw.async_turn_on()
        mock_set.assert_awaited_with(True)


async def test_button_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_button_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[button.ScpiPsuResetButton] = []
    await button.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == 1

    btn = added[0]
    with patch.object(coordinator, "async_reset_errors", new_callable=AsyncMock) as mock_reset:
        await btn.async_press()
        mock_reset.assert_awaited_once()


async def test_number_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025"},
        entry_id="test_number_entry",
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.data = ScpiPsuData(
        voltage_setpoint=5.0,
        current_setpoint=2.0,
        voltage_limit=30.0,
        current_limit=5.0,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list[number.ScpiPsuNumber] = []
    await number.async_setup_entry(
        hass, entry, MagicMock(side_effect=lambda ents: added.extend(ents))
    )
    assert len(added) == len(NUMBERS)

    num_map = {ent.entity_description.key: ent for ent in added}
    assert num_map["voltage_setpoint"].native_value == 5.0
    assert num_map["current_setpoint"].native_value == 2.0
    assert num_map["voltage_setpoint"].native_max_value == 60.0

    with patch.object(coordinator, "async_set_voltage", new_callable=AsyncMock) as mock_v:
        await num_map["voltage_setpoint"].async_set_native_value(12.0)
        mock_v.assert_awaited_once_with(12.0)
