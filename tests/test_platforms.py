"""Tests for SCPI PSU platforms (sensor, binary_sensor, switch, number, button)."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu.binary_sensor import BINARY_SENSORS, ScpiPsuBinarySensor
from custom_components.scpi_psu.button import ScpiPsuResetButton
from custom_components.scpi_psu.const import CONF_SERIAL_PORT, DOMAIN
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator, ScpiPsuData
from custom_components.scpi_psu.number import NUMBERS, ScpiPsuNumber
from custom_components.scpi_psu.sensor import SENSORS, ScpiPsuSensor
from custom_components.scpi_psu.switch import ScpiPsuOutputSwitch


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

    sensor_map = {desc.key: ScpiPsuSensor(coordinator, desc) for desc in SENSORS}
    assert sensor_map["voltage"].native_value == 12.34
    assert sensor_map["current"].native_value == 1.5
    assert sensor_map["power"].native_value == 18.51
    assert sensor_map["status"].native_value == "Active"
    assert sensor_map["error_register"].native_value == "0,No error"


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

    bin_map = {desc.key: ScpiPsuBinarySensor(coordinator, desc) for desc in BINARY_SENSORS}
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

    switch = ScpiPsuOutputSwitch(coordinator)
    assert switch.is_on is True

    with patch.object(coordinator, "async_set_output", new_callable=AsyncMock) as mock_set:
        await switch.async_turn_off()
        mock_set.assert_awaited_once_with(False)

        await switch.async_turn_on()
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

    button = ScpiPsuResetButton(coordinator)
    with patch.object(coordinator, "async_reset_errors", new_callable=AsyncMock) as mock_reset:
        await button.async_press()
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

    num_map = {desc.key: ScpiPsuNumber(coordinator, desc) for desc in NUMBERS}
    assert num_map["voltage_setpoint"].native_value == 5.0
    assert num_map["current_setpoint"].native_value == 2.0

    with patch.object(coordinator, "async_set_voltage", new_callable=AsyncMock) as mock_v:
        await num_map["voltage_setpoint"].async_set_native_value(12.0)
        mock_v.assert_awaited_once_with(12.0)
