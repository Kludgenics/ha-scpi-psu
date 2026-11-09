"""Comprehensive unit tests for scpi_psu_client (models, dialects, and client transport)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from serialx import SerialException

from custom_components.scpi_psu.scpi_psu_client import (
    OwonScpiProfile,
    PsuIdentity,
    ScpiPsuClient,
    StandardScpiProfile,
    parse_idn,
    parse_strict_float,
)


def test_parse_idn_owon() -> None:
    idn = "OWON,SPE6103,2138123,V1.2.3"
    ident = parse_idn(idn)
    assert ident.manufacturer == "OWON"
    assert ident.model == "SPE6103"
    assert ident.serial_number == "2138123"
    assert ident.firmware == "V1.2.3"
    assert ident.profile_key == "owon"
    assert ident.hw_max_voltage == 60.0
    assert ident.hw_max_current == 10.0
    assert ident.default_title == "OWON SPE6103"


def test_parse_idn_standard_generic() -> None:
    idn = "Rigol Technologies,DP832,DP8B1234567,00.01.14"
    ident = parse_idn(idn)
    assert ident.manufacturer == "Rigol Technologies"
    assert ident.model == "DP832"
    assert ident.profile_key == "standard"
    assert ident.hw_max_voltage == 30.0
    assert ident.hw_max_current == 3.0


def test_parse_idn_empty_and_partial() -> None:
    ident_none = parse_idn(None)
    assert ident_none.manufacturer == "SCPI"
    assert ident_none.model == "Power Supply"
    assert ident_none.profile_key == "standard"

    ident_blank_serial = parse_idn("Abestop,AT6301,0,")
    assert ident_blank_serial.serial_number is None
    assert ident_blank_serial.firmware is None
    assert ident_blank_serial.profile_key == "owon"

    ident_no_mfr = PsuIdentity(
        manufacturer="",
        model="CustomPSU",
        serial_number=None,
        firmware=None,
        raw_idn="",
        profile_key="standard",
        hw_max_voltage=30.0,
        hw_max_current=5.0,
    )
    assert ident_no_mfr.default_title == "CustomPSU"

    ident_completely_empty = PsuIdentity(
        manufacturer="",
        model="",
        serial_number=None,
        firmware=None,
        raw_idn="",
        profile_key="standard",
        hw_max_voltage=30.0,
        hw_max_current=5.0,
    )
    assert ident_completely_empty.default_title == "SCPI Power Supply"


def test_parse_strict_float() -> None:
    assert parse_strict_float("  12.345 \n") == 12.345
    assert parse_strict_float(None) is None
    assert parse_strict_float("") is None
    assert parse_strict_float("   \t\n  ") is None
    assert parse_strict_float("invalid") is None


def test_owon_profile_commands_and_parsing() -> None:
    profile = OwonScpiProfile()
    assert profile.format_set_voltage(12.3456) == "VOLT 12.346"
    assert profile.format_set_current(1.5) == "CURR 1.500"
    assert profile.format_set_voltage_limit(25.0) == "VOLT:LIM 25.000"
    assert profile.format_set_current_limit(3.2) == "CURR:LIM 3.200"
    assert profile.format_set_output(True) == "OUTP ON"
    assert profile.format_set_output(False) == "OUTP OFF"

    # Valid status codes: 0 (Standby), 1 (CV), 2 (CC), 3 (Fault), 99 (Other/Active)
    snap0 = profile.parse_meas_all_info("0.000, 0.000, 0.000, OFF, OFF, OFF, 0")
    assert snap0 is not None
    assert snap0.status_text == "Standby / Output Off"
    assert snap0.output_enabled is False
    assert snap0.fault_alarm is False

    snap1 = profile.parse_meas_all_info("12.000, 1.500, 18.000, 0, 0, 0, 1")
    assert snap1 is not None
    assert snap1.status_text == "Constant Voltage (CV)"
    assert snap1.output_enabled is True

    snap2 = profile.parse_meas_all_info("5.000, 2.000, 10.000, 0, 0, 0, 2")
    assert snap2 is not None
    assert snap2.status_text == "Constant Current (CC)"
    assert snap2.output_enabled is True

    snap3 = profile.parse_meas_all_info("0.000, 0.000, 0.000, ON, 0, 0, 3")
    assert snap3 is not None
    assert snap3.status_text == "Fault / Protection Trip"
    assert snap3.ovp_tripped is True
    assert snap3.fault_alarm is True
    assert snap3.output_enabled is None

    snap_other = profile.parse_meas_all_info("5.000, 1.000, 5.000, 0, 0, 0, 9")
    assert snap_other is not None
    assert snap_other.status_text == "Active"

    # Invalid cases
    assert profile.parse_meas_all_info(None) is None
    assert profile.parse_meas_all_info("") is None
    assert profile.parse_meas_all_info("12.0, 1.0, 12.0") is None  # Too few tokens
    assert profile.parse_meas_all_info("bad, 1.0, 12.0, 0, 0, 0") is None  # Non-float
    assert profile.parse_meas_all_info("999.0, 1.0, 12.0, 0, 0, 0") is None  # Out of range V
    assert profile.parse_meas_all_info("12.0, 1.0, 12.0, BAD_FLAG, 0, 0") is None  # Bad OVP flag


def test_standard_profile_commands() -> None:
    profile = StandardScpiProfile()
    assert profile.format_set_voltage(12.0) == "VOLT 12.000"
    assert profile.format_set_current(2.0) == "CURR 2.000"
    assert profile.format_set_voltage_limit(30.0) == "VOLT:PROT 30.000"
    assert profile.format_set_current_limit(5.0) == "CURR:PROT 5.000"
    assert profile.format_set_output(True) == "OUTP ON"
    assert profile.format_set_output(False) == "OUTP OFF"


async def test_client_transport_lifecycle_and_queries() -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025")
    assert client.is_connected is False
    assert client.profile.key == "standard"

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader._buffer = bytearray(b"stale")
    mock_reader.readline.return_value = b"OWON,SPE6103,12345,V1.0\r\n"

    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()

    with (
        patch(
            "custom_components.scpi_psu.scpi_psu_client.client.open_serial_connection",
            new_callable=AsyncMock,
            return_value=(mock_reader, mock_writer),
        ),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await client.connect()
        assert client.is_connected is True

        # Calling connect again reuses existing open writer
        await client.connect()
        assert client.is_connected is True

        # Probe identity
        mock_reader.read.side_effect = TimeoutError()
        ident = await client.probe_identity()
        assert ident.manufacturer == "OWON"
        assert client.profile.key == "owon"

        # Query with expect_reply=True
        mock_reader.readline.return_value = b"12.500\n"
        reply = await client.query("VOLT?")
        assert reply == "12.500"

        # Query with expect_reply=False
        reply_no_wait = await client.query("*CLS", expect_reply=False)
        assert reply_no_wait == ""

        # EOF on readline closes transport
        mock_reader.readline.return_value = b""
        eof_reply = await client.query("VOLT?")
        assert eof_reply is None
        assert client.is_connected is False

        # Reconnect and test TimeoutError on readline
        mock_reader.readline.side_effect = TimeoutError()
        timeout_reply = await client.query("VOLT?")
        assert timeout_reply is None

        # SerialException on write/readline closes transport
        mock_reader.readline.side_effect = SerialException("broken pipe")
        err_reply = await client.query("VOLT?")
        assert err_reply is None
        assert client.is_connected is False

        await client.connect()
        await client.close()
        assert client.is_connected is False


async def test_client_connection_failure() -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025")
    with patch(
        "custom_components.scpi_psu.scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=SerialException("Connection refused"),
    ):
        res = await client.query("*IDN?")
        assert res is None
        assert client.is_connected is False


async def test_client_configured_profile_override() -> None:
    # A standard IDN, but explicitly configured to use owon profile
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="CustomVendor,BenchSupply,123,1.0",
        profile_key="owon",
    )
    assert client.identity.profile_key == "standard"
    assert client.profile.key == "owon"


async def test_client_telemetry_and_controls() -> None:
    owon_client = ScpiPsuClient("socket://127.0.0.1:5025", initial_idn="OWON,SPE6103,1234,1.0")
    with patch.object(owon_client, "_query_unlocked", new_callable=AsyncMock) as mock_owon_q:
        mock_owon_q.return_value = "30.000, 2.500, 75.000, 0, 0, 0, 1"
        owon_snap = await owon_client.poll_telemetry()
        assert owon_snap is not None
        assert owon_snap.voltage == 30.0
        assert owon_snap.status_text == "Constant Voltage (CV)"

    client = ScpiPsuClient("socket://127.0.0.1:5025")
    with patch.object(client, "_query_unlocked", new_callable=AsyncMock) as mock_q:
        # Valid V, I, and missing P (computes V*I)
        mock_q.side_effect = ["10.0", "2.5", None]
        snap = await client.poll_telemetry()
        assert snap is not None
        assert snap.voltage == 10.0
        assert snap.current == 2.5
        assert snap.power == 25.0

        # Invalid V returns None
        mock_q.side_effect = [None]
        assert await client.poll_telemetry() is None

        # Invalid I returns None
        mock_q.side_effect = ["10.0", "999.0"]
        assert await client.poll_telemetry() is None

        # Output state queries
        mock_q.side_effect = None
        mock_q.return_value = "1"
        assert await client.get_output() is True
        mock_q.return_value = "0"
        assert await client.get_output() is False
        mock_q.return_value = "UNKNOWN"
        assert await client.get_output() is None

        # set_output branches
        mock_q.side_effect = ["", "ON"]
        assert await client.set_output(True) is True
        mock_q.side_effect = ["", "OFF"]
        assert await client.set_output(False) is False
        mock_q.side_effect = ["", None]
        assert await client.set_output(True) is True

        # set_voltage, set_current, set_voltage_limit, set_current_limit
        mock_q.side_effect = ["", "14.5"]
        assert await client.set_voltage(14.5) == 14.5
        mock_q.side_effect = ["", "3.2"]
        assert await client.set_current(3.2) == 3.2
        mock_q.side_effect = ["", "28.0"]
        assert await client.set_voltage_limit(28.0) == 28.0
        mock_q.side_effect = ["", "5.5"]
        assert await client.set_current_limit(5.5) == 5.5

        # get_system_error, get_setpoints, get_config_registers, clear_faults
        mock_q.side_effect = None
        mock_q.return_value = "0,No error"
        assert await client.get_system_error() == "0,No error"
        mock_q.return_value = "ON"
        assert await client.get_system_error() is None

        mock_q.side_effect = ["12.0", "2.0"]
        setpoints = await client.get_setpoints()
        assert setpoints == {
            "voltage_setpoint": 12.0,
            "current_setpoint": 2.0,
        }

        mock_q.side_effect = ["30.0", "5.0", "12.0", "2.0"]
        regs = await client.get_config_registers()
        assert regs == {
            "voltage_limit": 30.0,
            "current_limit": 5.0,
            "voltage_setpoint": 12.0,
            "current_setpoint": 2.0,
        }

        mock_q.side_effect = None
        mock_q.return_value = ""
        await client.clear_faults()
        mock_q.assert_called_with("*CLS", expect_reply=False)
