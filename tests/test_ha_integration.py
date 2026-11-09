"""Tests for SCPI PSU integration setup, unload, USB scanner, and coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu import (
    _scan_remote_serial_ports,
    async_reload_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.scpi_psu.const import (
    CONF_BAUDRATE,
    CONF_IDN,
    CONF_MAX_CURRENT_CLAMP,
    CONF_MAX_VOLTAGE_CLAMP,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator
from custom_components.scpi_psu.scpi_psu_client.models import TelemetrySnapshot, parse_idn


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="OWON SPE6103",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_BAUDRATE: 115200,
            CONF_SCAN_INTERVAL: 2,
            CONF_IDN: "OWON,SPE6103,1234,V1.0",
            CONF_MAX_VOLTAGE_CLAMP: 24.0,
            CONF_MAX_CURRENT_CLAMP: 5.0,
        },
        entry_id="test_scpi_entry",
    )


async def test_integration_setup_and_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config.components.add("usb")

    with patch(
        "custom_components.scpi_psu.async_register_serial_port_scanner"
    ) as mock_register_scanner:
        assert await async_setup(hass, {}) is True
        mock_register_scanner.assert_called_once_with(hass, _scan_remote_serial_ports)

    snap = TelemetrySnapshot(
        voltage=12.0,
        current=1.0,
        power=12.0,
        output_enabled=True,
        status_text="Constant Voltage (CV)",
    )

    with (
        patch(
            "custom_components.scpi_psu.scpi_psu_client.client.ScpiPsuClient.poll_all_channels",
            new_callable=AsyncMock,
            return_value={1: snap},
        ),
        patch(
            "custom_components.scpi_psu.scpi_psu_client.client.ScpiPsuClient.get_config_registers",
            new_callable=AsyncMock,
            return_value={
                "voltage_limit": 60.0,
                "current_limit": 10.0,
                "voltage_setpoint": 12.0,
                "current_setpoint": 1.0,
            },
        ),
        patch(
            "custom_components.scpi_psu.scpi_psu_client.client.ScpiPsuClient.get_system_error",
            new_callable=AsyncMock,
            return_value="0,No error",
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_forward,
        patch.object(
            hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True
        ) as mock_unload,
        patch.object(hass.config_entries, "async_reload", new_callable=AsyncMock) as mock_reload,
    ):
        from homeassistant.config_entries import ConfigEntryState

        mock_config_entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        assert await async_setup_entry(hass, mock_config_entry) is True
        mock_forward.assert_awaited_once()

        # Verify USB remote serial port scanner
        scanned = _scan_remote_serial_ports(hass)
        assert len(scanned) == 1
        assert scanned[0].device == "socket://127.0.0.1:5025"
        assert scanned[0].manufacturer == "OWON"

        # Test reload helper
        await async_reload_entry(hass, mock_config_entry)
        mock_reload.assert_awaited_once_with(mock_config_entry.entry_id)

        # Test unload
        assert await async_unload_entry(hass, mock_config_entry) is True
        mock_unload.assert_awaited_once()
        assert mock_config_entry.entry_id not in hass.data[DOMAIN]


async def test_coordinator_update_and_unresponsive_recovery(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)

    assert coordinator.effective_max_voltage == 24.0
    assert coordinator.effective_max_current == 5.0
    assert coordinator.profile.key == "owon"

    snap = TelemetrySnapshot(
        voltage=12.5,
        current=1.2,
        power=15.0,
        output_enabled=True,
        status_text="Constant Voltage (CV)",
    )

    with (
        patch.object(coordinator.client, "poll_all_channels", new_callable=AsyncMock) as mock_poll,
        patch.object(
            coordinator.client, "get_config_registers", new_callable=AsyncMock
        ) as mock_regs,
        patch.object(coordinator.client, "get_setpoints", new_callable=AsyncMock) as mock_setpoints,
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock) as mock_err,
        patch.object(coordinator.client, "close", new_callable=AsyncMock) as mock_close,
    ):
        mock_poll.return_value = {1: snap}
        mock_regs.return_value = {
            "voltage_limit": 60.0,
            "current_limit": 10.0,
            "voltage_setpoint": 12.5,
            "current_setpoint": 1.2,
        }
        mock_setpoints.return_value = {
            "voltage_setpoint": 13.0,
            "current_setpoint": 1.5,
        }
        mock_err.return_value = "0,No error"

        await coordinator.async_refresh()
        data = coordinator.data
        assert data.responding is True
        assert data.ch1.voltage == 12.5
        assert data.ch1.current == 1.2
        assert data.ch1.power == 15.0
        assert data.ch1.output_enabled is True
        assert data.voltage_setpoint == 12.5

        # Subsequent fast poll syncs setpoints without full config poll
        coordinator.client.identity = parse_idn("Rigol Technologies,DP832,123,1.0")
        snap_std = TelemetrySnapshot(
            voltage=12.5,
            current=1.2,
            power=15.0,
            output_enabled=False,
            status_text="Standby / Output Off",
        )
        mock_poll.return_value = {1: snap_std}
        await coordinator.async_refresh()
        data_std = coordinator.data
        assert data_std.ch1.status_text == "Standby / Output Off"
        assert data_std.voltage_setpoint == 13.0
        assert data_std.current_setpoint == 1.5

        mock_poll.return_value = None
        await coordinator.async_refresh()
        assert coordinator.data.responding is True

        await coordinator.async_refresh()
        assert coordinator.data.responding is False
        assert coordinator.data.ch1.voltage is None
        assert coordinator.data.ch1.status_text == "Unresponsive / Power Lost"

        await coordinator.async_refresh()
        assert coordinator.data.responding is False
        mock_close.assert_awaited_once()


async def test_coordinator_control_methods_and_clamps(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    from tests.test_scpi_client import mock_serial_responder

    mock_config_entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry, command_delay=0.0, write_delay=0.0)

    sent_commands: list[str] = []
    state: dict[str, str] = {
        "MEAS:ALL:INFO?": "24.000, 2.000, 48.000, 0, 0, 0, 1",
        "OUTP?": "ON",
        "VOLT?": "24.000",
        "CURR?": "5.000",
        "VOLT:LIM?": "24.000",
        "CURR:LIM?": "5.000",
        "SYST:ERR?": "0,No error",
    }

    def _responder(cmd: str) -> str | None:
        sent_commands.append(cmd)
        return state.get(cmd, "")

    with mock_serial_responder(_responder):
        await coordinator.async_set_output(True)
        assert coordinator.data.ch1.output_enabled is True
        assert coordinator.data.ch1.voltage == 24.0

        # Request 50V when clamp is 24V -> should clamp to 24.0
        await coordinator.async_set_voltage(50.0)
        assert "VOLT 24.000" in sent_commands
        assert coordinator.data.voltage_setpoint == 24.0

        # Request 10A when clamp is 5A -> should clamp to 5.0
        await coordinator.async_set_current(10.0)
        assert "CURR 5.000" in sent_commands
        assert coordinator.data.current_setpoint == 5.0

        # Request 50V OVP limit when clamp is 24V -> should clamp to 24.0
        await coordinator.async_set_voltage_limit(50.0)
        assert "VOLT:LIM 24.000" in sent_commands
        assert coordinator.data.voltage_limit == 24.0

        # Request 10A OCP limit when clamp is 5A -> should clamp to 5.0
        await coordinator.async_set_current_limit(10.0)
        assert "CURR:LIM 5.000" in sent_commands
        assert coordinator.data.current_limit == 5.0

        # Reset errors
        await coordinator.async_reset_errors()
        assert "*CLS" in sent_commands
        assert coordinator.data.system_error == "0,No error"

        await coordinator.async_close()
        assert coordinator.data.connected is False
