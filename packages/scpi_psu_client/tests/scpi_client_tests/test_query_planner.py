"""Tests for Deterministic Query Planner and Compound Queries."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scpi_psu_client import ScpiPsuClient
from scpi_psu_client.dialects import OwonScpiProfile, StandardScpiProfile
from scpi_psu_client.query_planner import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_OUTPUT_ENABLED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    QueryPlanner,
)


def test_query_planner_owon_profile() -> None:
    owon_planner = QueryPlanner(OwonScpiProfile(), has_dmm=True)

    # When multiple fields are requested, OWON selects composite MEAS:ALL:INFO?
    plan = owon_planner.plan_queries({FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER, FIELD_STATUS_TEXT})
    assert len(plan) == 1
    assert plan[0].command == "MEAS:ALL:INFO?"

    assert plan[0].parser is not None
    parsed = plan[0].parser("12.5, 1.2, 15.0, 0, 0, 0, 1")
    assert parsed[FIELD_VOLTAGE] == 12.5
    assert parsed[FIELD_CURRENT] == 1.2
    assert parsed[FIELD_POWER] == 15.0
    assert parsed[FIELD_STATUS_TEXT] == "Constant Voltage (CV)"

    # When DMM is also requested, DMM is appended as an independent descriptor
    dmm_plan = owon_planner.plan_queries({FIELD_VOLTAGE, FIELD_DMM})
    assert len(dmm_plan) == 2
    assert dmm_plan[0].command == "MEAS:ALL:INFO?"
    assert dmm_plan[1].command == ":READ?"


def test_query_planner_standard_scpi_compound_pipeline() -> None:
    std_planner = QueryPlanner(StandardScpiProfile(), has_dmm=False)

    # When multiple fields are needed on Standard SCPI, they are bundled into 1 semicolon compound query
    plan_std = std_planner.plan_queries(
        {FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER, FIELD_OUTPUT_ENABLED}
    )
    assert len(plan_std) == 1
    assert plan_std[0].command == "MEAS:VOLT?;:MEAS:CURR?;:MEAS:POW?;:OUTP?"

    # Semicolon parser cleanly unpacks the compound response
    assert plan_std[0].parser is not None
    parsed = plan_std[0].parser("15.500 ; 2.300 ; 35.650 ; ON")
    assert parsed[FIELD_VOLTAGE] == 15.5
    assert parsed[FIELD_CURRENT] == 2.3
    assert parsed[FIELD_POWER] == 35.65
    assert parsed[FIELD_OUTPUT_ENABLED] is True

    # When only 1 scalar field is needed, it remains a single discrete query (no semicolons)
    single_plan = std_planner.plan_queries({FIELD_VOLTAGE})
    assert len(single_plan) == 1
    assert single_plan[0].command == "MEAS:VOLT?"


async def test_partial_telemetry_and_channel_field_interest() -> None:
    """Verify polling only a subset of fields (e.g. voltage disabled or channel 2 output only)."""
    from scpi_psu_client.client import ScpiPsuClient

    from .serial_responder import mock_serial_responder

    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol Technologies,DP832,123,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    queried_commands: list[str] = []

    def _responder(cmd: str) -> str | None:
        queried_commands.append(cmd)
        if cmd == "MEAS:CURR?":
            return "1.75"
        if "OUTP?" in cmd:
            return "ON"
        if cmd == "MEAS:VOLT?;:OUTP?":
            return "12.000;ON"
        return ""

    with mock_serial_responder(_responder):
        # 1. Poll only current (voltage sensor disabled by user) -> succeeds with voltage=None
        snap_curr = await client.poll_telemetry(channel=1, needed_fields={FIELD_CURRENT})
        assert snap_curr is not None
        assert snap_curr.current == 1.75
        assert snap_curr.voltage is None
        assert snap_curr.power is None
        assert [cmd for cmd in queried_commands if "?" in cmd] == ["MEAS:CURR?"]

        # 2. Poll only status_text on Standard SCPI -> covered by OUTP? without needing voltage/current
        queried_commands.clear()
        snap_status = await client.poll_telemetry(channel=1, needed_fields={FIELD_STATUS_TEXT})
        assert snap_status is not None
        assert snap_status.output_enabled is True
        assert snap_status.status_text == "Active"
        assert [cmd for cmd in queried_commands if "?" in cmd] == ["OUTP?"]

        # 3. Poll all channels with per-channel field map (CH1: current, CH2: output_enabled)
        queried_commands.clear()
        all_snaps = await client.poll_all_channels(
            needed_fields_by_channel={
                1: {FIELD_CURRENT},
                2: {FIELD_OUTPUT_ENABLED},
            }
        )
        assert all_snaps is not None
        assert set(all_snaps.keys()) == {1, 2}
        assert all_snaps[1].current == 1.75
        assert all_snaps[2].output_enabled is True
        assert all_snaps[2].voltage is None
        assert [cmd for cmd in queried_commands if "?" in cmd] == ["MEAS:CURR?", "OUTP? CH2"]


async def test_status_is_unknown_when_output_reply_is_unparseable() -> None:
    from .serial_responder import mock_serial_responder

    client = ScpiPsuClient("socket://127.0.0.1:5025", command_delay=0, write_delay=0)
    with mock_serial_responder(lambda cmd: "12.0;invalid"):
        snap = await client.poll_telemetry(needed_fields={FIELD_VOLTAGE, FIELD_STATUS_TEXT})

    assert snap is not None
    assert snap.voltage == 12.0
    assert snap.output_enabled is None
    assert snap.status_text is None


async def test_channel_selection_is_restored_after_poll() -> None:
    from scpi_psu_client.client import ScpiPsuClient

    from .serial_responder import mock_serial_responder

    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1.0",
        command_delay=0.0,
        write_delay=0.0,
    )
    selected = 3
    written_channels: list[int] = []

    def responder(cmd: str) -> str:
        nonlocal selected
        if cmd.startswith("INST:NSEL "):
            selected = int(cmd.rsplit(" ", 1)[1])
        elif cmd.startswith("VOLT "):
            written_channels.append(selected)
        elif cmd == "VOLT?":
            return "4.0"
        elif cmd.startswith("OUTP?"):
            return "ON"
        return "invalid"

    with mock_serial_responder(responder):
        snapshot = await client.poll_telemetry(2, {FIELD_OUTPUT_ENABLED})
        assert snapshot is not None and snapshot.output_enabled is True
        await client.set_voltage(4.0)
        assert written_channels == [1]
        assert await client.poll_all_channels({1: {FIELD_VOLTAGE}}) is None
        await client.set_voltage(4.0)
        assert written_channels == [1, 1]


def test_compound_query_error_and_corrupt_parsing() -> None:
    """Verify compound query parsing resilience against missing, empty, or garbage tokens."""
    std_planner = QueryPlanner(StandardScpiProfile())
    plan = std_planner.plan_queries({FIELD_VOLTAGE, FIELD_CURRENT, FIELD_OUTPUT_ENABLED})
    assert len(plan) == 1
    parser = plan[0].parser
    assert parser is not None

    # Normal success
    res_good = parser("12.000 ; 1.500 ; ON")
    assert res_good[FIELD_VOLTAGE] == 12.0
    assert res_good[FIELD_CURRENT] == 1.5
    assert res_good[FIELD_OUTPUT_ENABLED] is True

    # Truncated response (fewer tokens than queries) -> safely drops frame
    assert parser("12.000 ; 1.500") == {}
    # Extra tokens -> safely drops frame
    assert parser("12.000 ; 1.500 ; ON ; EXTRA") == {}
    # Empty string or None -> safely drops frame
    assert parser("") == {}
    assert parser(None) == {}

    # Corrupt scalar within compound -> only valid scalar fields are extracted
    res_bad_curr = parser("12.000 ; GARBAGE ; ON")
    assert res_bad_curr[FIELD_VOLTAGE] == 12.0
    assert FIELD_CURRENT not in res_bad_curr
    assert res_bad_curr[FIELD_OUTPUT_ENABLED] is True


@pytest.mark.parametrize(
    ("identity", "expected_query"),
    [("Rigol,DP832,123,1", b"VOLT?;:CURR?\n"), ("OWON,SPE6103,123,1", b"VOLT?\n")],
)
async def test_register_poll_stops_after_transport_failure(
    identity: str, expected_query: bytes
) -> None:
    client = ScpiPsuClient("socket://127.0.0.1:5025", initial_idn=identity, write_delay=0)
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = TimeoutError()
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        assert await client.get_setpoints() == {
            "voltage_setpoint": None,
            "current_setpoint": None,
        }
        connect.assert_awaited_once()
        expected_commands = [expected_query]
        if identity.startswith("Rigol"):
            expected_commands.insert(0, b"INST:NSEL 1\n")
        assert [call.args[0] for call in writer.write.call_args_list] == expected_commands


@pytest.mark.parametrize("telemetry", [True, False])
async def test_malformed_compound_restarts_session_and_uses_scalar_queries(telemetry: bool) -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1" if telemetry else "Rigol,DP711,123,1",
        command_delay=0,
        write_delay=0,
    )
    old_reader = AsyncMock(spec=asyncio.StreamReader)
    old_reader.readline.side_effect = [b"12\n", b"999\n"]
    new_reader = AsyncMock(spec=asyncio.StreamReader)
    new_reader.readline.side_effect = [
        (client.identity.raw_idn + "\n").encode(),
        b"12\n",
        b"1\n",
        b"13\n",
        b"2\n",
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
    ) as connect:
        if telemetry:
            snap = await client.poll_telemetry(2, {FIELD_VOLTAGE, FIELD_CURRENT})
            assert snap is not None
            assert (snap.voltage, snap.current, snap.channel) == (12.0, 1.0, 2)
            snap = await client.poll_telemetry(2, {FIELD_VOLTAGE, FIELD_CURRENT})
            assert snap is not None
            assert (snap.voltage, snap.current, snap.channel) == (13.0, 2.0, 2)
            expected_queries = [b"MEAS:VOLT?\n", b"MEAS:CURR?\n"] * 2
        else:
            assert await client.get_setpoints() == {
                "voltage_setpoint": 12.0,
                "current_setpoint": 1.0,
            }
            assert await client.get_setpoints() == {
                "voltage_setpoint": 13.0,
                "current_setpoint": 2.0,
            }
            expected_queries = [b"VOLT?\n", b"CURR?\n"] * 2
        assert connect.await_count == 2
        old_reader.readline.assert_awaited_once()
        writers[0].close.assert_called_once()
        queries = [call.args[0] for call in writers[1].write.call_args_list if b"?" in call.args[0]]
        assert queries == [b"*IDN?\n", *expected_queries]


async def test_malformed_register_reply_does_not_read_a_different_channel() -> None:
    client = ScpiPsuClient(
        "socket://127.0.0.1:5025",
        initial_idn="Rigol,DP832,123,1",
        command_delay=0,
        write_delay=0,
    )
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.return_value = b"12\n"
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ) as connect:
        assert await client.select_channel(2)
        assert await client.get_setpoints() == {
            "voltage_setpoint": None,
            "current_setpoint": None,
        }
        assert not client.is_connected
        connect.assert_awaited_once()
        assert [call.args[0] for call in writer.write.call_args_list] == [
            b"INST:NSEL 1\n",
            b"INST:NSEL 2\n",
            b"VOLT?;:CURR?\n",
        ]
