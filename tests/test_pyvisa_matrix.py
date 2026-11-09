"""Matrix compatibility tests using pyvisa-sim and TCP bridge across diverse SCPI PSU devices."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu import (
    async_setup_entry,
    async_unload_entry,
    binary_sensor,
    button,
    number,
    sensor,
    switch,
)
from custom_components.scpi_psu.const import (
    CONF_BAUDRATE,
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator
from custom_components.scpi_psu.scpi_psu_client import ScpiPsuClient
from tests.sim_bridge import PyVisaSimTcpBridge

SIM_YAML = str(Path(__file__).parent / "sim_devices.yaml")

DEVICE_MATRIX = [
    (
        "ASRL1::INSTR",
        "OWON",
        "SPE6103",
        "owon",
        60.0,
        10.0,
        {"voltage": 12.0, "current": 2.0, "power": 24.0, "status": "Constant Voltage (CV)"},
    ),
    (
        "ASRL2::INSTR",
        "Abestop",
        "AT6301",
        "owon",
        60.0,
        10.0,
        {"voltage": 5.0, "current": 1.2, "power": 6.0, "status": "Constant Voltage (CV)"},
    ),
    (
        "ASRL3::INSTR",
        "Rigol Technologies",
        "DP832",
        "standard",
        30.0,
        3.0,
        {"voltage": 24.0, "current": 1.5, "power": 36.0, "status": "Active"},
    ),
    (
        "ASRL4::INSTR",
        "Siglent Technologies",
        "SPD3303X",
        "standard",
        32.0,
        3.2,
        {"voltage": 18.0, "current": 2.0, "power": 36.0, "status": "Active"},
    ),
    (
        "ASRL5::INSTR",
        "Keysight Technologies",
        "E36311A",
        "standard",
        25.0,
        5.0,
        {"voltage": 15.0, "current": 1.0, "power": 15.0, "status": "Active"},
    ),
]


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    (
        "resource_id",
        "expected_mfr",
        "expected_model",
        "expected_profile",
        "max_v",
        "max_i",
        "expected_telemetry",
    ),
    DEVICE_MATRIX,
)
async def test_client_compatibility_matrix(
    resource_id: str,
    expected_mfr: str,
    expected_model: str,
    expected_profile: str,
    max_v: float,
    max_i: float,
    expected_telemetry: dict[str, Any],
) -> None:
    """Test pure-python client against each simulated hardware model in the matrix."""
    bridge = PyVisaSimTcpBridge(resource_id, SIM_YAML)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url)

    try:
        ident = await client.probe_identity()
        assert ident.manufacturer == expected_mfr
        assert ident.model == expected_model
        assert ident.profile_key == expected_profile
        assert ident.hw_max_voltage == max_v
        assert ident.hw_max_current == max_i

        telemetry = await client.poll_telemetry()
        assert telemetry is not None
        assert telemetry.voltage == expected_telemetry["voltage"]
        assert telemetry.current == expected_telemetry["current"]
        assert telemetry.power == expected_telemetry["power"]

        regs = await client.get_config_registers()
        assert regs["voltage_setpoint"] is not None
        assert regs["current_setpoint"] is not None

        err = await client.get_system_error()
        assert err == "0,No error"

        outp = await client.get_output()
        assert outp is True
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    (
        "resource_id",
        "expected_mfr",
        "expected_model",
        "expected_profile",
        "max_v",
        "max_i",
        "expected_telemetry",
    ),
    DEVICE_MATRIX,
)
async def test_ha_integration_compatibility_matrix(
    hass: HomeAssistant,
    resource_id: str,
    expected_mfr: str,
    expected_model: str,
    expected_profile: str,
    max_v: float,
    max_i: float,
    expected_telemetry: dict[str, Any],
) -> None:
    """Test full Home Assistant integration and entity platforms against simulated hardware."""
    bridge = PyVisaSimTcpBridge(resource_id, SIM_YAML)
    socket_url = await bridge.start()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"{expected_mfr} {expected_model}",
        data={
            CONF_SERIAL_PORT: socket_url,
            CONF_BAUDRATE: 115200,
        },
        entry_id=f"test_sim_{expected_model.lower()}",
    )
    entry.add_to_hass(hass)

    from homeassistant.config_entries import ConfigEntryState

    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)

    try:
        with (
            patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock),
            patch.object(
                hass.config_entries,
                "async_unload_platforms",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert await async_setup_entry(hass, entry) is True
            coordinator: ScpiPsuCoordinator = hass.data[DOMAIN][entry.entry_id]

            assert coordinator.identity.manufacturer == expected_mfr
            assert coordinator.identity.model == expected_model
            assert coordinator.data.voltage == expected_telemetry["voltage"]
            assert coordinator.data.current == expected_telemetry["current"]
            assert coordinator.data.power == expected_telemetry["power"]
            assert coordinator.data.status_text == expected_telemetry["status"]

            # Verify platform entities instantiate cleanly against coordinator data
            sensors: list[sensor.ScpiPsuSensor] = []
            await sensor.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: sensors.extend(ents))
            )
            sensor_dict = {s.entity_description.key: s.native_value for s in sensors}
            assert sensor_dict["voltage"] == expected_telemetry["voltage"]
            assert sensor_dict["current"] == expected_telemetry["current"]
            assert sensor_dict["power"] == expected_telemetry["power"]

            bin_sensors: list[binary_sensor.ScpiPsuBinarySensor] = []
            await binary_sensor.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: bin_sensors.extend(ents))
            )
            bin_dict = {b.entity_description.key: b.is_on for b in bin_sensors}
            assert bin_dict["connected"] is True

            numbers: list[number.ScpiPsuNumber] = []
            await number.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: numbers.extend(ents))
            )
            num_dict = {n.entity_description.key: n.native_max_value for n in numbers}
            assert num_dict["voltage_setpoint"] == max_v

            buttons: list[button.ScpiPsuResetButton] = []
            await button.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: buttons.extend(ents))
            )
            assert len(buttons) == 1

            switches: list[switch.ScpiPsuOutputSwitch] = []
            await switch.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: switches.extend(ents))
            )
            assert len(switches) == 1
            assert switches[0].is_on is True

            # Unload integration
            assert await async_unload_entry(hass, entry) is True
    finally:
        await bridge.stop()
