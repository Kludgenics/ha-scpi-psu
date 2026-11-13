"""Comprehensive unit tests for scpi_psu_client (models, dialects, and client transport)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scpi_psu_client import (
    OwonScpiProfile,
    PsuIdentity,
    ScpiPsuClient,
    StandardScpiProfile,
    parse_idn,
    parse_strict_float,
)
from serialx import SerialException

from .serial_responder import mock_serial_responder


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

    assert profile.parse_composite_telemetry(None) is None
    assert profile.parse_composite_telemetry("") is None
    assert profile.parse_composite_telemetry("12.0, 1.0, 12.0") is None
    assert profile.parse_composite_telemetry("bad, 1.0, 12.0, 0, 0, 0") is None
    assert profile.parse_composite_telemetry("999.0, 1.0, 12.0, 0, 0, 0") is None
    assert profile.parse_composite_telemetry("12.0, 1.0, 12.0, BAD_FLAG, 0, 0") is None


def test_standard_profile_commands() -> None:
    profile = StandardScpiProfile()
    assert profile.format_set_voltage(12.0) == "VOLT 12.000"
    assert profile.format_set_current(2.0) == "CURR 2.000"
    assert profile.format_set_voltage_limit(30.0) == "VOLT:PROT 30.000"
    assert profile.format_set_current_limit(5.0) == "CURR:PROT 5.000"
    assert profile.format_set_output(True) == "OUTP ON"
    assert profile.format_set_output(False) == "OUTP OFF"


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
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(mock_reader, mock_writer),
    ):
        await client.connect()
        assert client.is_connected is True

        await client.connect()
        assert client.is_connected is True

        ident = await client.probe_identity()
        assert ident.manufacturer == "OWON"
        assert client.profile.key == "owon"

        mock_reader.readline.return_value = b"12.500\n"
        reply = await client.query("VOLT?")
        assert reply == "12.500"

        reply_no_wait = await client.query("*CLS", expect_reply=False)
        assert reply_no_wait == ""

        mock_reader.readline.return_value = b""
        eof_reply = await client.query("VOLT?")
        assert eof_reply is None
        assert client.is_connected is False

        mock_reader.readline.side_effect = TimeoutError()
        timeout_reply = await client.query("VOLT?")
        assert timeout_reply is None
        assert client.is_connected is False

        mock_reader.readline.side_effect = SerialException("broken pipe")
        err_reply = await client.query("VOLT?")
        assert err_reply is None
        assert client.is_connected is False

        await client.connect()
        await client.close()
        assert client.is_connected is False


@pytest.mark.parametrize("error", [TimeoutError(), asyncio.CancelledError()])
async def test_poll_disconnects_after_interrupted_reply_without_reconnecting(
    error: BaseException,
) -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = error
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await client.poll_telemetry(2, {"output_enabled"})
        else:
            assert await client.poll_telemetry(2, {"output_enabled"}) is None
        assert not client.is_connected
        connect.assert_awaited_once()
        assert writer.write.call_args.args[0] == b"OUTP? CH2\n"


async def test_multichannel_poll_stops_after_transport_timeout() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = [b"ON\n", TimeoutError()]
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        fields = {channel: {"output_enabled"} for channel in (1, 2, 3)}
        assert await client.poll_all_channels(fields) is None
        connect.assert_awaited_once()
        assert [call.args[0] for call in writer.write.call_args_list if b"?" in call.args[0]] == [
            b"OUTP?\n",
            b"OUTP? CH2\n",
        ]
        assert writer.write.call_args.args[0] == b"OUTP? CH2\n"


@pytest.mark.parametrize("operation", ["telemetry", "registers"])
async def test_compound_timeout_uses_scalar_queries_on_next_poll(operation: str) -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP711,123,1.0",
        command_delay=0,
        write_delay=0,
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = [TimeoutError(), b"Rigol,DP711,123,1.0\n", b"12\n", b"2\n"]
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        if operation == "telemetry":
            assert await client.poll_telemetry(needed_fields={"voltage", "current"}) is None
        else:
            assert await client.get_setpoints() == {
                "voltage_setpoint": None,
                "current_setpoint": None,
            }
        connect.assert_awaited_once()
        assert writer.write.call_count == 1

        if operation == "telemetry":
            snapshot = await client.poll_telemetry(needed_fields={"voltage", "current"})
            assert snapshot is not None
            assert (snapshot.voltage, snapshot.current) == (12.0, 2.0)
        else:
            assert await client.get_setpoints() == {
                "voltage_setpoint": 12.0,
                "current_setpoint": 2.0,
            }
        assert connect.await_count == 2
        commands = [call.args[0] for call in writer.write.call_args_list]
        assert b";" in commands[0]
        assert all(b";" not in command for command in commands[1:])


async def test_reconnect_resets_retained_channel_before_setpoint() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    selected = 2
    written_channels: list[int] = []

    def responder(cmd: str) -> str | None:
        nonlocal selected
        if cmd == "*IDN?":
            return "Rigol,DP832,123,1.0"
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd == "OUTP? CH2":
            return None
        elif cmd.startswith("VOLT "):
            written_channels.append(selected)
        elif cmd == "VOLT?":
            return "4.0"
        return ""

    with mock_serial_responder(responder):
        assert await client.poll_telemetry(2, {"output_enabled"}) is None
        assert selected == 2
        assert await client.set_voltage(4.0) == 4.0
        assert written_channels == [1]


async def test_discovery_resets_selected_channel() -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0, write_delay=0)
    selected = 2
    written_channels: list[int] = []

    def responder(cmd: str) -> str:
        nonlocal selected
        if cmd == "*IDN?":
            return "Rigol,DP832,123,1.0"
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd.startswith("VOLT "):
            written_channels.append(selected)
        elif cmd == "VOLT?":
            return "4.0"
        return ""

    with mock_serial_responder(responder):
        assert (await client.probe_identity()).channels == 3
        await client.set_voltage(4.0)
        assert written_channels == [1]


async def test_failed_reprobe_preserves_known_identity_for_reconnection() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0,
        write_delay=0,
    )
    selected = 1
    written_channels: list[int] = []

    def responder(cmd: str) -> str | None:
        nonlocal selected
        if cmd == "*IDN?":
            return None
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd.startswith("VOLT "):
            written_channels.append(selected)
        elif cmd == "VOLT?":
            return "4.0"
        return ""

    with mock_serial_responder(responder):
        assert await client.select_channel(2)
        assert (await client.probe_identity()).channels == 3
        with pytest.raises(ConnectionError):
            await client.set_voltage(4.0)
        assert written_channels == []
        assert client.identity.channels == 3

    unknown = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0, write_delay=0)
    with mock_serial_responder(lambda _: None):
        identity = await unknown.probe_identity()
        assert identity.raw_idn == ""
        assert identity.channels == 1


async def test_cancelled_connection_initialization_closes_transport() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0,
        write_delay=60,
    )
    opened = asyncio.Event()
    selected = 2
    written_channels: list[int] = []
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.return_value = b"4.0\n"
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    def on_write(data: bytes) -> None:
        nonlocal selected
        cmd = data.decode().strip()
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd.startswith("VOLT "):
            written_channels.append(selected)

    writer.write.side_effect = on_write

    async def open_connection(**kwargs: object) -> tuple[AsyncMock, MagicMock]:
        opened.set()
        return reader, writer

    with patch(
        "scpi_psu_client.client.open_serial_connection",
        side_effect=open_connection,
    ):
        task = asyncio.create_task(client.connect())
        await opened.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.is_connected
        writer.close.assert_called_once()
        client.write_delay = 0
        assert await client.set_voltage(4.0) == 4.0
        assert written_channels == [1]


async def test_channel_one_output_commands_ignore_previous_channel_selection() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0,
        write_delay=0,
    )
    selected = 1
    output_channels: list[int] = []
    voltage_channels: list[int] = []

    def responder(cmd: str) -> str:
        nonlocal selected
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd == "OUTP ON":
            output_channels.append(selected)
        elif cmd.startswith("VOLT "):
            voltage_channels.append(selected)
        elif cmd == "OUTP?":
            return "ON" if selected == 1 else "OFF"
        elif cmd == "VOLT?":
            return "4.0"
        return ""

    with mock_serial_responder(responder):
        assert await client.select_channel(2)
        assert await client.set_voltage(4.0) == 4.0
        assert voltage_channels == [2]
        assert await client.set_output(True, channel=1)
        assert await client.select_channel(2)
        assert await client.get_output(channel=1) is True
        assert await client.select_channel(2)
        assert await client.set_all_outputs(True)
        assert output_channels == [1, 1]


@pytest.mark.parametrize("method", ["set_voltage", "set_output", "set_all_outputs", "clear_faults"])
async def test_failed_write_does_not_reconnect_or_report_success(method: str) -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0, write_delay=0)
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock(side_effect=SerialException("write failed"))
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        args = () if method == "clear_faults" else ((4.0,) if method == "set_voltage" else (True,))
        with pytest.raises(ConnectionError):
            await getattr(client, method)(*args)
        connect.assert_awaited_once()
        writer.write.assert_called_once()


async def test_failed_multi_output_write_stops_remaining_commands() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0,
        write_delay=0,
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock(side_effect=[None, None, None, SerialException("write failed")])
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        with pytest.raises(ConnectionError):
            await client.set_all_outputs(True)
        connect.assert_awaited_once()
        assert [call.args[0] for call in writer.write.call_args_list] == [
            b"INST:NSEL 1\n",
            b"INST:NSEL 1\n",
            b"OUTP:ALL ON\n",
            b"OUTP ON\n",
        ]


async def test_client_connection_failure() -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025")
    with patch(
        "scpi_psu_client.client.open_serial_connection",
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
    with mock_serial_responder(lambda cmd: "30.000, 2.500, 75.000, 0, 0, 0, 1"):
        owon_snap = await owon_client.poll_telemetry()
        assert owon_snap is not None
        assert owon_snap.voltage == 30.0
        assert owon_snap.status_text == "Constant Voltage (CV)"

    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0.0, write_delay=0.0)
    std_table = {
        "MEAS:VOLT?;:MEAS:CURR?;:MEAS:POW?;:OUTP?;:VOLT:PROT:TRIP?;:CURR:PROT:TRIP?": "10.0;2.5;25.0;ON;0;0",
        "OUTP?": "ON",
        "MEAS:VOLT?": "10.0",
        "MEAS:CURR?": "2.5",
        "MEAS:POW?": "25.0",
    }
    with mock_serial_responder(lambda cmd: std_table.get(cmd)):
        snap = await client.poll_telemetry()
        assert snap is not None
        assert snap.voltage == 10.0
        assert snap.current == 2.5
        assert snap.power == 25.0
        assert snap.output_enabled is True
        assert snap.ovp_tripped is False
        assert snap.ocp_tripped is False

    await client.close()
    with mock_serial_responder(lambda cmd: "ON" if "OUTP" in cmd else "bad"):
        assert await client.poll_telemetry() is None

    await client.close()
    with mock_serial_responder(
        lambda cmd: "999.0" if "CURR" in cmd else ("10.0" if "VOLT" in cmd else "ON")
    ):
        assert await client.poll_telemetry() is None

    state: dict[str, str] = {"OUTP?": "1"}

    def _dispatch_query(cmd: str) -> str | None:
        if cmd == "*IDN?":
            return "Generic,PSU,123,1.0"
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

    await client.close()
    with mock_serial_responder(_dispatch_query) as writer:
        assert await client.get_output() is True
        state["OUTP?"] = "0"
        assert await client.get_output() is False
        state["OUTP?"] = "UNKNOWN"
        assert await client.get_output() is None

        assert await client.set_output(True) is True
        assert await client.set_output(False) is False

        state["VOLT?"] = "14.5"
        assert await client.set_voltage(14.5) == 14.5
        state["CURR?"] = "3.2"
        assert await client.set_current(3.2) == 3.2
        state["VOLT:PROT?"] = "28.0"
        assert await client.set_voltage_limit(28.0) == 28.0
        state["CURR:PROT?"] = "5.5"
        assert await client.set_current_limit(5.5) == 5.5

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

        state["VOLT:PROT?"] = "30.0"
        state["CURR:PROT?"] = "5.0"
        regs = await client.get_config_registers()
        assert regs == {
            "voltage_limit": 30.0,
            "current_limit": 5.0,
            "voltage_setpoint": 12.0,
            "current_setpoint": 2.0,
        }

        await client.clear_faults()
        writer.write.assert_called_with(b"*CLS\n")


@pytest.mark.parametrize("misplaced", [b"54.190,1.635,88.600,OFF,OFF,OFF,1\n", b"5.455\n", b"\n"])
async def test_error_reply_recovers_past_delayed_bridge_data(misplaced: bytes) -> None:
    identity = "OWON,SPE6103,123,1.0"
    client = ScpiPsuClient(
        "socket://bridge:4001", initial_idn=identity, command_delay=0, write_delay=0
    )
    old_reader = AsyncMock(spec=asyncio.StreamReader)
    old_reader.readline.return_value = misplaced
    new_reader = AsyncMock(spec=asyncio.StreamReader)
    new_reader.readline.side_effect = [
        b"0,No error\n",
        b"5.455\n",
        identity.encode() + b"\n",
        b"0,No error\n",
        b"12.0\n",
    ]
    writers = [MagicMock(), MagicMock()]
    for writer in writers:
        writer.is_closing.return_value = False
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[(old_reader, writers[0]), (new_reader, writers[1])],
    ):
        assert await client.get_system_error() is None
        assert not client.is_connected
        assert await client.get_system_error() == "0,No error"
        assert await client.query("VOLT?") == "12.0"
        assert writers[1].write.call_args_list[0].args[0] == b"*IDN?\n"
    await client.close()


async def test_error_recovery_does_not_issue_query_without_identity_boundary() -> None:
    client = ScpiPsuClient(
        "socket://bridge:4001", initial_idn="OWON,SPE6103,123,1.0", command_delay=0, write_delay=0
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = [b"5.455\n", b"5.455\n", b""]
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ):
        assert await client.get_system_error() is None
        assert await client.query("VOLT?") is None
        assert not client.is_connected
        assert b"VOLT?\n" not in [call.args[0] for call in writer.write.call_args_list]


@pytest.mark.parametrize("reply", ["0x0000", "0x001A", '0,"No error"'])
async def test_system_error_accepts_hex_register_without_losing_alignment(reply: str) -> None:
    client = ScpiPsuClient(
        "socket://bridge:4001", initial_idn="OWON,SPE6103,123,1.0", command_delay=0, write_delay=0
    )
    with mock_serial_responder(
        lambda command: reply if command == "SYST:ERR?" else "12.345"
    ) as writer:
        assert await client.get_system_error() == reply
        assert client.is_connected
        assert await client.query("VOLT?") == "12.345"
        assert [call.args[0] for call in writer.write.call_args_list] == [
            b"SYST:ERR?\n",
            b"VOLT?\n",
        ]
    await client.close()
