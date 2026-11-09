"""Comprehensive unit tests for scpi_psu_client (models, dialects, and client transport)."""

from __future__ import annotations

import asyncio
from typing import Any
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
    snap0 = profile.parse_composite_telemetry("0.000, 0.000, 0.000, OFF, OFF, OFF, 0")
    assert snap0 is not None
    assert snap0.status_text == "Standby / Output Off"
    assert snap0.output_enabled is False
    assert snap0.fault_alarm is False

    snap1 = profile.parse_composite_telemetry("12.000, 1.500, 18.000, 0, 0, 0, 1")
    assert snap1 is not None
    assert snap1.status_text == "Constant Voltage (CV)"
    assert snap1.output_enabled is True

    snap2 = profile.parse_composite_telemetry("5.000, 2.000, 10.000, 0, 0, 0, 2")
    assert snap2 is not None
    assert snap2.status_text == "Constant Current (CC)"
    assert snap2.output_enabled is True

    snap3 = profile.parse_composite_telemetry("0.000, 0.000, 0.000, ON, 0, 0, 3")
    assert snap3 is not None
    assert snap3.status_text == "Fault / Protection Trip"
    assert snap3.ovp_tripped is True
    assert snap3.fault_alarm is True
    assert snap3.output_enabled is None

    snap_other = profile.parse_composite_telemetry("5.000, 1.000, 5.000, 0, 0, 0, 9")
    assert snap_other is not None
    assert snap_other.status_text == "Active"

    # Invalid cases
    assert profile.parse_composite_telemetry(None) is None
    assert profile.parse_composite_telemetry("") is None
    assert profile.parse_composite_telemetry("12.0, 1.0, 12.0") is None  # Too few tokens
    assert profile.parse_composite_telemetry("bad, 1.0, 12.0, 0, 0, 0") is None  # Non-float
    assert profile.parse_composite_telemetry("999.0, 1.0, 12.0, 0, 0, 0") is None  # Out of range V
    assert (
        profile.parse_composite_telemetry("12.0, 1.0, 12.0, BAD_FLAG, 0, 0") is None
    )  # Bad OVP flag


def test_standard_profile_commands() -> None:
    profile = StandardScpiProfile()
    assert profile.format_set_voltage(12.0) == "VOLT 12.000"
    assert profile.format_set_current(2.0) == "CURR 2.000"
    assert profile.format_set_voltage_limit(30.0) == "VOLT:PROT 30.000"
    assert profile.format_set_current_limit(5.0) == "CURR:PROT 5.000"
    assert profile.format_set_output(True) == "OUTP ON"
    assert profile.format_set_output(False) == "OUTP OFF"


def _attach_responder(client: ScpiPsuClient, responder: Any) -> MagicMock:
    """Attach a command-driven transport responder to a client instance."""
    pending_line = b""

    def _on_write(data: bytes) -> None:
        nonlocal pending_line
        cmd = data.decode("ascii", errors="ignore").strip()
        ans = responder(cmd)
        pending_line = f"{ans}\r\n".encode("ascii") if ans is not None else b""

    async def _on_readline() -> bytes:
        return pending_line

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readline.side_effect = _on_readline
    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_writer.write.side_effect = _on_write
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()

    client._reader = mock_reader
    client._writer = mock_writer
    client._connected = True
    return mock_writer


async def test_client_transport_lifecycle_and_queries() -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0.0, write_delay=0.0)
    assert client.is_connected is False
    assert client.profile.key == "standard"

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readline.return_value = b"OWON,SPE6103,12345,V1.0\r\n"

    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()

    with patch(
        "custom_components.scpi_psu.scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(mock_reader, mock_writer),
    ):
        await client.connect()
        assert client.is_connected is True

        # Calling connect again reuses existing open writer
        await client.connect()
        assert client.is_connected is True

        # Probe identity
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
    owon_client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="OWON,SPE6103,1234,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    _attach_responder(owon_client, lambda cmd: "30.000, 2.500, 75.000, 0, 0, 0, 1")
    owon_snap = await owon_client.poll_telemetry()
    assert owon_snap is not None
    assert owon_snap.voltage == 30.0
    assert owon_snap.status_text == "Constant Voltage (CV)"

    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0.0, write_delay=0.0)
    std_table = {
        "OUTP?": "ON",
        "MEAS:VOLT?": "10.0",
        "MEAS:CURR?": "2.5",
        "MEAS:POW?": "25.0",
    }
    _attach_responder(client, lambda cmd: std_table.get(cmd))
    snap = await client.poll_telemetry()
    assert snap is not None
    assert snap.voltage == 10.0
    assert snap.current == 2.5
    assert snap.power == 25.0
    assert snap.output_enabled is True

    # Invalid V returns None
    _attach_responder(client, lambda cmd: "ON" if "OUTP" in cmd else "bad")
    assert await client.poll_telemetry() is None

    # Invalid I returns None
    _attach_responder(
        client,
        lambda cmd: "999.0" if "CURR" in cmd else ("10.0" if "VOLT" in cmd else "ON"),
    )
    assert await client.poll_telemetry() is None

    state: dict[str, str] = {"OUTP?": "1"}

    def _dispatch_query(cmd: str) -> str | None:
        if cmd == "OUTP ON":
            state["OUTP?"] = "ON"
            return ""
        if cmd == "OUTP OFF":
            state["OUTP?"] = "OFF"
            return ""
        if cmd == "OUTP?":
            return state.get("OUTP?", "ON")
        if cmd in ("VOLT?", "VOLT:LIM?", "VOLT:PROT?"):
            return state.get(cmd, "14.5")
        if cmd in ("CURR?", "CURR:LIM?", "CURR:PROT?"):
            return state.get(cmd, "3.2")
        if cmd == "SYST:ERR?":
            return state.get(cmd, "0,No error")
        return ""

    writer = _attach_responder(client, _dispatch_query)

    assert await client.get_output() is True
    state["OUTP?"] = "0"
    assert await client.get_output() is False
    state["OUTP?"] = "UNKNOWN"
    assert await client.get_output() is None

    assert await client.set_output(True) is True
    assert await client.set_output(False) is False

    # set_voltage, set_current, set_voltage_limit, set_current_limit
    state["VOLT?"] = "14.5"
    assert await client.set_voltage(14.5) == 14.5
    state["CURR?"] = "3.2"
    assert await client.set_current(3.2) == 3.2
    state[client.profile.cmd_get_voltage_limit] = "28.0"
    assert await client.set_voltage_limit(28.0) == 28.0
    state[client.profile.cmd_get_current_limit] = "5.5"
    assert await client.set_current_limit(5.5) == 5.5

    # get_system_error, get_setpoints, get_config_registers, clear_faults
    state["SYST:ERR?"] = "0,No error"
    assert await client.get_system_error() == "0,No error"
    state["SYST:ERR?"] = ""
    assert await client.get_system_error() is None

    state["VOLT?"] = "12.0"
    state["CURR?"] = "2.0"
    setpoints = await client.get_setpoints()
    assert setpoints == {
        "voltage_setpoint": 12.0,
        "current_setpoint": 2.0,
    }

    state[client.profile.cmd_get_voltage_limit] = "30.0"
    state[client.profile.cmd_get_current_limit] = "5.0"
    regs = await client.get_config_registers()
    assert regs == {
        "voltage_limit": 30.0,
        "current_limit": 5.0,
        "voltage_setpoint": 12.0,
        "current_setpoint": 2.0,
    }

    await client.clear_faults()
    writer.write.assert_called_with(b"*CLS\n")
