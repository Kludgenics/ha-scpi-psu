"""Tests for Deterministic Query Planner and Compound Queries."""

from __future__ import annotations

from custom_components.scpi_psu.scpi_psu_client.dialects import OwonScpiProfile, StandardScpiProfile
from custom_components.scpi_psu.scpi_psu_client.query_planner import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_OUTPUT_ENABLED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    QueryPlanner,
)


def test_query_planner_owon_profile() -> None:
    owon_planner = QueryPlanner(OwonScpiProfile(), channels=1, has_dmm=True)

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
    std_planner = QueryPlanner(StandardScpiProfile(), channels=1, has_dmm=False)

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
    from custom_components.scpi_psu.scpi_psu_client.client import ScpiPsuClient
    from tests.test_scpi_client import _attach_responder

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

    _attach_responder(client, _responder)

    # 1. Poll only current (voltage sensor disabled by user) -> succeeds with voltage=None
    snap_curr = await client.poll_telemetry(channel=1, needed_fields={FIELD_CURRENT})
    assert snap_curr is not None
    assert snap_curr.current == 1.75
    assert snap_curr.voltage is None
    assert snap_curr.power is None
    assert queried_commands == ["INST:NSEL 1", "MEAS:CURR?"]

    # 2. Poll only status_text on Standard SCPI -> covered by OUTP? without needing voltage/current
    queried_commands.clear()
    snap_status = await client.poll_telemetry(channel=1, needed_fields={FIELD_STATUS_TEXT})
    assert snap_status is not None
    assert snap_status.output_enabled is True
    assert snap_status.status_text == "Active"
    assert queried_commands == ["INST:NSEL 1", "OUTP?"]

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


def test_compound_query_error_and_corrupt_parsing() -> None:
    """Verify compound query parsing resilience against missing, empty, or garbage tokens."""
    std_planner = QueryPlanner(StandardScpiProfile(), channels=1)
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
