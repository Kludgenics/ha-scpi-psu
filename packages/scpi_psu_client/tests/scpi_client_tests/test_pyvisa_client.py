"""Standalone client compatibility against simulated instruments."""

from typing import Any

import pytest
from scpi_psu_client import ScpiPsuClient

from .matrix import DEVICE_MATRIX, SIM_YAML
from .sim_bridge import PyVisaSimTcpBridge


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

        target_v = round(min(max_v * 0.5, 12.0), 2)
        v_res = await client.set_voltage(target_v)
        assert v_res == target_v

        target_i = round(min(max_i * 0.5, 1.5), 2)
        i_res = await client.set_current(target_i)
        assert i_res == target_i
        assert await client.get_setpoints() == {
            "voltage_setpoint": target_v,
            "current_setpoint": target_i,
        }

        assert await client.set_output(False) is False
        assert await client.get_output() is False
        assert await client.set_output(True) is True
        assert await client.get_output() is True

        v_lim_res = await client.set_voltage_limit(max_v)
        assert v_lim_res == max_v
        i_lim_res = await client.set_current_limit(max_i)
        assert i_lim_res == max_i
        registers = await client.get_config_registers()
        assert registers["voltage_limit"] == max_v
        assert registers["current_limit"] == max_i

        if expected_channels > 1:
            for ch in range(1, expected_channels + 1):
                assert await client.select_channel(ch) is True
    finally:
        await client.close()
        await bridge.stop()
