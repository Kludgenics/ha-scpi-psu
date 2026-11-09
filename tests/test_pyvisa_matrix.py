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
        1,
        False,
        {
            "voltage": 12.0,
            "current": 2.0,
            "power": 24.0,
            "status": "Constant Voltage (CV)",
            "dmm": None,
        },
    ),
    (
        "ASRL2::INSTR",
        "Abestop",
        "AT6301",
        "owon",
        60.0,
        10.0,
        1,
        False,
        {
            "voltage": 5.0,
            "current": 1.2,
            "power": 6.0,
            "status": "Constant Voltage (CV)",
            "dmm": None,
        },
    ),
    (
        "ASRL3::INSTR",
        "Rigol Technologies",
        "DP832",
        "standard",
        30.0,
        3.0,
        3,
        False,
        {"voltage": 24.0, "current": 1.5, "power": 36.0, "status": "Active", "dmm": None},
    ),
    (
        "ASRL4::INSTR",
        "Siglent Technologies",
        "SPD3303X",
        "standard",
        32.0,
        3.2,
        3,
        False,
        {"voltage": 18.0, "current": 2.0, "power": 36.0, "status": "Active", "dmm": None},
    ),
    (
        "ASRL5::INSTR",
        "Keysight Technologies",
        "E36311A",
        "standard",
        25.0,
        5.0,
        3,
        False,
        {"voltage": 15.0, "current": 1.0, "power": 15.0, "status": "Active", "dmm": None},
    ),
    (
        "ASRL6::INSTR",
        "OWON",
        "SPM6103",
        "owon",
        60.0,
        10.0,
        1,
        True,
        {
            "voltage": 12.0,
            "current": 1.5,
            "power": 18.0,
            "status": "Constant Voltage (CV)",
            "dmm": 3.298,
        },
    ),
    (
        "ASRL7::INSTR",
        "ROHDE&SCHWARZ",
        "HMP4040",
        "standard",
        32.0,
        10.0,
        4,
        False,
        {"voltage": 32.0, "current": 10.0, "power": 320.0, "status": "Active", "dmm": None},
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
        "expected_channels",
        "expected_has_dmm",
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
    expected_channels: int,
    expected_has_dmm: bool,
    expected_telemetry: dict[str, Any],
) -> None:
    """Test pure-python client against each simulated hardware model in the matrix."""
    bridge = PyVisaSimTcpBridge(resource_id, SIM_YAML)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0)

    try:
        ident = await client.probe_identity()
        assert ident.manufacturer == expected_mfr
        assert ident.model == expected_model
        assert ident.profile_key == expected_profile
        assert ident.hw_max_voltage == max_v
        assert ident.hw_max_current == max_i
        assert ident.channels == expected_channels
        assert ident.has_dmm == expected_has_dmm

        telemetry = await client.poll_telemetry()
        assert telemetry is not None
        assert telemetry.voltage == expected_telemetry["voltage"]
        assert telemetry.current == expected_telemetry["current"]
        assert telemetry.power == expected_telemetry["power"]
        if expected_has_dmm:
            assert telemetry.dmm_reading == expected_telemetry["dmm"]

        regs = await client.get_config_registers()
        assert regs["voltage_setpoint"] is not None
        assert regs["current_setpoint"] is not None

        err = await client.get_system_error()
        assert err == "0,No error"

        outp = await client.get_output()
        assert outp is True

        # Test active control setpoints & readback round-trip
        target_v = round(min(max_v * 0.5, 12.0), 2)
        v_res = await client.set_voltage(target_v)
        assert v_res == target_v

        target_i = round(min(max_i * 0.5, 1.5), 2)
        i_res = await client.set_current(target_i)
        assert i_res == target_i

        # Test output toggling round-trip
        assert await client.set_output(False) is False
        assert await client.set_output(True) is True

        # Test voltage and current protection limit setpoints
        v_lim_res = await client.set_voltage_limit(max_v)
        assert v_lim_res == max_v
        i_lim_res = await client.set_current_limit(max_i)
        assert i_lim_res == max_i

        if expected_channels > 1:
            for ch in range(1, expected_channels + 1):
                assert await client.select_channel(ch) is True
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
        "expected_channels",
        "expected_has_dmm",
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
    expected_channels: int,
    expected_has_dmm: bool,
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
            patch(
                "custom_components.scpi_psu.ScpiPsuCoordinator",
                side_effect=lambda h, e: ScpiPsuCoordinator(
                    h, e, command_delay=0.0, write_delay=0.0
                ),
            ),
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
            assert coordinator.identity.channels == expected_channels
            assert coordinator.identity.has_dmm == expected_has_dmm
            assert coordinator.data.voltage == expected_telemetry["voltage"]
            assert coordinator.data.current == expected_telemetry["current"]
            assert coordinator.data.power == expected_telemetry["power"]
            assert coordinator.data.status_text == expected_telemetry["status"]
            if expected_has_dmm:
                assert coordinator.data.dmm_reading == expected_telemetry["dmm"]

            # Verify platform entities instantiate cleanly against coordinator data
            sensors: list[sensor.ScpiPsuSensor] = []
            await sensor.async_setup_entry(
                hass, entry, MagicMock(side_effect=lambda ents: sensors.extend(ents))
            )
            sensor_dict = {s.entity_description.key: s.native_value for s in sensors}
            assert sensor_dict["voltage"] == expected_telemetry["voltage"]
            assert sensor_dict["current"] == expected_telemetry["current"]
            assert sensor_dict["power"] == expected_telemetry["power"]
            if expected_has_dmm:
                assert "dmm" in sensor_dict
                assert sensor_dict["dmm"] == expected_telemetry["dmm"]
            else:
                assert "dmm" not in sensor_dict

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

            # Verify child device hierarchy mapping
            parent_id = coordinator.parent_device_id
            assert parent_id is not None
            if expected_channels > 1:
                sw_info = switches[0].device_info
                assert sw_info is not None
                assert isinstance(sw_info, dict)
                assert sw_info.get("parent_device_id") == parent_id
                assert (DOMAIN, f"{entry.entry_id}_ch1") in sw_info.get("identifiers", set())
                for s in sensors:
                    if s.entity_description.key in ("voltage", "current", "power", "status"):
                        s_info = s.device_info
                        assert s_info is not None
                        assert isinstance(s_info, dict)
                        assert s_info.get("parent_device_id") == parent_id
            else:
                sw_info = switches[0].device_info
                assert sw_info is not None
                assert isinstance(sw_info, dict)
                assert "parent_device_id" not in sw_info
                assert (DOMAIN, entry.entry_id) in sw_info.get("identifiers", set())

            if expected_has_dmm:
                dmm_sensor = next(s for s in sensors if s.entity_description.key == "dmm")
                dmm_info = dmm_sensor.device_info
                assert dmm_info is not None
                assert isinstance(dmm_info, dict)
                assert dmm_info.get("parent_device_id") == parent_id
                assert (DOMAIN, f"{entry.entry_id}_dmm") in dmm_info.get("identifiers", set())

            # Exercise coordinator actions (turn off output, change voltage setpoint)
            await switches[0].async_turn_off()
            assert switches[0].is_on is False
            await switches[0].async_turn_on()
            assert switches[0].is_on is True

            volt_num = next(n for n in numbers if n.entity_description.key == "voltage_setpoint")
            new_v = round(min(max_v * 0.4, 10.0), 2)
            await volt_num.async_set_native_value(new_v)
            assert coordinator.data.voltage_setpoint == new_v

            # Unload integration
            assert await async_unload_entry(hass, entry) is True
    finally:
        await bridge.stop()
