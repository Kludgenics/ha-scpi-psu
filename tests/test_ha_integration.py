"""Tests for SCPI PSU integration setup, unload, USB scanner, and coordinator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu import (
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
from custom_components.scpi_psu.scpi_psu_client import (
    FIELD_DMM,
    FIELD_OUTPUT_ENABLED,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
)
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
        mock_register_scanner.assert_called_once()
        assert mock_register_scanner.call_args[0][0] is hass
        scanner = mock_register_scanner.call_args[0][1]

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

        scanned = scanner(hass)
        assert len(scanned) == 1
        assert scanned[0].device == "socket://127.0.0.1:5025"
        assert scanned[0].manufacturer == "OWON"

        await async_reload_entry(hass, mock_config_entry)
        mock_reload.assert_awaited_once_with(mock_config_entry.entry_id)

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
        dmm_reading=3.3,
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
        assert data.all_outputs_enabled is True
        assert data.dmm_reading == 3.3
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
        assert coordinator.data.all_outputs_enabled is None
        assert coordinator.data.dmm_reading is None

        await coordinator.async_refresh()
        assert coordinator.data.responding is False
        mock_close.assert_awaited_once()


async def test_failed_secondary_channel_clears_only_requested_fields(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Three channel PSU",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_IDN: "Rigol,DP832,123,1.0",
        },
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)

    with (
        patch.object(coordinator.client, "poll_all_channels", new_callable=AsyncMock) as poll,
        patch.object(
            coordinator.client,
            "get_config_registers",
            new_callable=AsyncMock,
            return_value={
                "voltage_limit": 30.0,
                "current_limit": 3.0,
                "voltage_setpoint": 12.0,
                "current_setpoint": 1.0,
            },
        ),
        patch.object(
            coordinator.client,
            "get_setpoints",
            new_callable=AsyncMock,
            return_value={"voltage_setpoint": 12.0, "current_setpoint": 1.0},
        ),
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock),
    ):
        poll.return_value = {
            1: TelemetrySnapshot(channel=1, output_enabled=False),
            2: TelemetrySnapshot(channel=2, voltage=12.0, output_enabled=True),
            3: TelemetrySnapshot(channel=3, output_enabled=True),
        }
        await coordinator.async_refresh()
        coordinator.register_field_interest(FIELD_OUTPUT_ENABLED, channel=1)
        coordinator.register_field_interest(FIELD_OUTPUT_ENABLED, channel=2)

        poll.return_value = {
            1: TelemetrySnapshot(channel=1, output_enabled=False),
            2: TelemetrySnapshot(channel=2, voltage=13.0),
        }
        await coordinator.async_refresh()
        assert coordinator.data.channels[2].output_enabled is None
        assert coordinator.data.channels[2].voltage == 13.0

        poll.return_value[2] = TelemetrySnapshot(channel=2, output_enabled=True)
        await coordinator.async_refresh()
        poll.return_value = {1: TelemetrySnapshot(channel=1, output_enabled=False)}
        await coordinator.async_refresh()
        assert coordinator.data.channels[2].output_enabled is None
        assert coordinator.data.channels[2].voltage == 13.0
        assert coordinator.data.channels[3].output_enabled is True

        coordinator.register_field_interest(FIELD_OUTPUT_ENABLED, channel=3)
        poll.return_value = {
            1: TelemetrySnapshot(channel=1, output_enabled=False),
            3: TelemetrySnapshot(channel=3, output_enabled=False),
        }
        await coordinator.async_refresh()
        assert coordinator.data.all_outputs_enabled is None


async def test_partial_primary_poll_clears_only_requested_fields(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PSU with multimeter",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_IDN: "OWON,SPM6103,123,1.0",
        },
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    coordinator.register_field_interest(FIELD_STATUS_TEXT)
    with (
        patch.object(coordinator.client, "poll_all_channels", new_callable=AsyncMock) as poll,
        patch.object(
            coordinator.client,
            "get_config_registers",
            new_callable=AsyncMock,
            return_value={
                "voltage_limit": 60.0,
                "current_limit": 10.0,
                "voltage_setpoint": 12.0,
                "current_setpoint": 1.0,
            },
        ),
        patch.object(
            coordinator.client,
            "get_setpoints",
            new_callable=AsyncMock,
            return_value={"voltage_setpoint": 12.0, "current_setpoint": 1.0},
        ),
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock),
    ):
        poll.return_value = {
            1: TelemetrySnapshot(voltage=12.0, current=1.5, output_enabled=True, dmm_reading=3.3)
        }
        await coordinator.async_refresh()
        assert coordinator.data.ch1.status_text is None
        for field in (FIELD_OUTPUT_ENABLED, FIELD_VOLTAGE, FIELD_DMM):
            coordinator.register_field_interest(field)

        poll.return_value = {1: TelemetrySnapshot(voltage=11.5)}
        await coordinator.async_refresh()
        assert coordinator.data.ch1.output_enabled is None
        assert coordinator.data.ch1.voltage == 11.5
        assert coordinator.data.ch1.current == 1.5
        assert coordinator.data.dmm_reading is None

        poll.return_value = None
        await coordinator.async_refresh()
        assert coordinator.data.responding is True
        assert coordinator.data.ch1.voltage == 11.5
        assert coordinator.data.ch1.current == 1.5
        await coordinator.async_refresh()
        assert coordinator.data.responding is False
        assert coordinator.data.ch1.output_enabled is None


@pytest.mark.parametrize("failure", [ConnectionError("write failed"), asyncio.CancelledError()])
async def test_failed_setpoint_write_marks_only_that_value_unknown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, failure: BaseException
) -> None:
    mock_config_entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)
    with (
        patch.object(coordinator.client, "set_voltage", new_callable=AsyncMock, return_value=12.0),
        patch.object(coordinator.client, "set_current", new_callable=AsyncMock, return_value=1.0),
    ):
        await coordinator.async_set_voltage(12.0)
        await coordinator.async_set_current(1.0)

    published: list[tuple[float | None, float | None]] = []
    unsubscribe = coordinator.async_add_listener(
        lambda: published.append(
            (coordinator.data.voltage_setpoint, coordinator.data.current_setpoint)
        )
    )
    with (
        patch.object(
            coordinator.client, "set_voltage", new_callable=AsyncMock, side_effect=failure
        ),
        pytest.raises(type(failure)),
    ):
        await coordinator.async_set_voltage(15.0)
    unsubscribe()
    assert published == [(None, 1.0)]


@pytest.mark.parametrize("failure", [ConnectionError("write failed"), asyncio.CancelledError()])
async def test_failed_output_write_marks_only_that_channel_unknown(
    hass: HomeAssistant, failure: BaseException
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Three channel PSU",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_IDN: "Rigol,DP832,123,1.0",
        },
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    with (
        patch.object(coordinator.client, "set_output", new_callable=AsyncMock, return_value=False),
        patch.object(
            coordinator.client, "poll_telemetry", new_callable=AsyncMock, return_value=None
        ),
    ):
        await coordinator.async_set_output(False, channel=1)
        await coordinator.async_set_output(False, channel=2)

    published: list[tuple[bool | None, bool | None]] = []
    unsubscribe = coordinator.async_add_listener(
        lambda: published.append(
            (
                coordinator.data.channels[1].output_enabled,
                coordinator.data.channels[2].output_enabled,
            )
        )
    )

    with (
        patch.object(coordinator.client, "set_output", new_callable=AsyncMock, side_effect=failure),
        pytest.raises(type(failure)),
    ):
        await coordinator.async_set_output(True, channel=2)

    unsubscribe()
    assert published == [(False, None)]
    assert coordinator.data.channels[2].status_text is None
    assert coordinator.data.all_outputs_enabled is None


@pytest.mark.parametrize("failure", [ConnectionError("write failed"), asyncio.CancelledError()])
async def test_partial_all_outputs_write_marks_states_unknown(
    hass: HomeAssistant, failure: BaseException
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Three channel PSU",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_IDN: "Rigol,DP832,123,1.0",
        },
    )
    entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, entry)
    snapshots = {
        ch: TelemetrySnapshot(channel=ch, voltage=12.0, output_enabled=False, status_text="Off")
        for ch in range(1, 4)
    }
    with (
        patch.object(
            coordinator.client, "poll_all_channels", new_callable=AsyncMock, return_value=snapshots
        ),
        patch.object(
            coordinator.client,
            "get_config_registers",
            new_callable=AsyncMock,
            return_value={
                "voltage_limit": 30.0,
                "current_limit": 3.0,
                "voltage_setpoint": 12.0,
                "current_setpoint": 1.0,
            },
        ),
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock),
    ):
        await coordinator.async_refresh()

    published: list[tuple[bool | None, ...]] = []
    unsubscribe = coordinator.async_add_listener(
        lambda: published.append(
            tuple(coordinator.data.channels[ch].output_enabled for ch in range(1, 4))
        )
    )
    with (
        patch.object(
            coordinator.client, "set_all_outputs", new_callable=AsyncMock, side_effect=failure
        ),
        pytest.raises(type(failure)),
    ):
        await coordinator.async_set_all_outputs(True)

    unsubscribe()
    assert published == [(None, None, None)]
    assert coordinator.data.all_outputs_enabled is None
    assert all(channel.status_text is None for channel in coordinator.data.channels.values())
    assert coordinator.data.ch1.voltage == 12.0


async def test_coordinator_control_methods_and_clamps(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    from scpi_client_tests.serial_responder import mock_serial_responder

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

    with mock_serial_responder(
        _responder, client_module="custom_components.scpi_psu.scpi_psu_client.client"
    ):
        await coordinator.async_set_output(True)
        assert coordinator.data.ch1.output_enabled is True
        assert coordinator.data.ch1.voltage == 24.0

        await coordinator.async_set_voltage(50.0)
        assert "VOLT 24.000" in sent_commands
        assert coordinator.data.voltage_setpoint == 24.0

        await coordinator.async_set_current(10.0)
        assert "CURR 5.000" in sent_commands
        assert coordinator.data.current_setpoint == 5.0

        await coordinator.async_set_voltage_limit(50.0)
        assert "VOLT:LIM 24.000" in sent_commands
        assert coordinator.data.voltage_limit == 24.0

        await coordinator.async_set_current_limit(10.0)
        assert "CURR:LIM 5.000" in sent_commands
        assert coordinator.data.current_limit == 5.0

        await coordinator.async_reset_errors()
        assert "*CLS" in sent_commands
        assert coordinator.data.system_error == "0,No error"

        await coordinator.async_close()
        assert coordinator.data.connected is False


async def test_failed_setup_closes_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)
    with (
        patch("custom_components.scpi_psu.ScpiPsuCoordinator", return_value=coordinator),
        patch.object(
            coordinator,
            "async_config_entry_first_refresh",
            side_effect=RuntimeError("setup failed"),
        ),
        patch.object(coordinator, "async_close", new_callable=AsyncMock) as close,
    ):
        with pytest.raises(RuntimeError, match="setup failed"):
            await async_setup_entry(hass, mock_config_entry)
        close.assert_awaited_once()
        assert mock_config_entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_error_query_disconnect_updates_connection_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    from unittest.mock import PropertyMock

    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)
    with (
        patch.object(
            coordinator.client,
            "poll_all_channels",
            return_value={1: TelemetrySnapshot(voltage=12.0)},
        ),
        patch.object(
            coordinator.client,
            "get_config_registers",
            return_value=dict.fromkeys(
                ("voltage_limit", "current_limit", "voltage_setpoint", "current_setpoint")
            ),
        ),
        patch.object(coordinator.client, "get_system_error", return_value=None),
        patch.object(
            type(coordinator.client), "is_connected", new_callable=PropertyMock, return_value=False
        ),
    ):
        await coordinator.async_refresh()
        assert coordinator.data.connected is False
        assert coordinator.data.system_error is None


@pytest.mark.parametrize("failure", [ConnectionError("write failed"), asyncio.CancelledError()])
@pytest.mark.parametrize("step", ["clear_faults", "poll_telemetry", "get_system_error"])
async def test_failed_error_reset_publishes_unknown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, failure: BaseException, step: str
) -> None:
    mock_config_entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)
    with (
        patch.object(
            coordinator.client,
            "poll_all_channels",
            return_value={1: TelemetrySnapshot(voltage=12.0)},
        ),
        patch.object(
            coordinator.client,
            "get_config_registers",
            return_value=dict.fromkeys(
                ("voltage_limit", "current_limit", "voltage_setpoint", "current_setpoint")
            ),
        ),
        patch.object(coordinator.client, "get_system_error", return_value='-200,"Execution error"'),
    ):
        await coordinator.async_refresh()
    assert coordinator.data.system_error == '-200,"Execution error"'
    published: list[str | None] = []
    unsubscribe = coordinator.async_add_listener(
        lambda: published.append(coordinator.data.system_error)
    )
    with (
        patch.object(coordinator.client, "clear_faults", new_callable=AsyncMock),
        patch.object(
            coordinator.client, "poll_telemetry", new_callable=AsyncMock, return_value=None
        ),
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock),
        patch.object(coordinator.client, step, new_callable=AsyncMock, side_effect=failure),
        pytest.raises(type(failure)),
    ):
        await coordinator.async_reset_errors()
    unsubscribe()
    assert published == [None]
