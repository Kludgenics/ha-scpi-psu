"""Tests for Weighted Set Cover Query Planner and Latency Tracker."""

from __future__ import annotations

from custom_components.scpi_psu.scpi_psu_client.dialects import OwonScpiProfile, StandardScpiProfile
from custom_components.scpi_psu.scpi_psu_client.query_planner import (
    FIELD_CURRENT,
    FIELD_OUTPUT_ENABLED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    LatencyTracker,
    QueryDescriptor,
    QueryPlanner,
    solve_weighted_set_cover,
)


def test_latency_tracker_ewma() -> None:
    tracker = LatencyTracker(alpha=0.5, default_latency_ms=20.0, timeout_penalty_ms=500.0)
    assert tracker.get_latency("MEAS:VOLT?") == 20.0

    tracker.record_latency("MEAS:VOLT?", 10.0, success=True)
    # 1st sample sets initial
    assert tracker.get_latency("MEAS:VOLT?") == 10.0

    # 2nd sample: 0.5 * 30 + 0.5 * 10 = 20.0
    tracker.record_latency("MEAS:VOLT?", 30.0, success=True)
    assert tracker.get_latency("MEAS:VOLT?") == 20.0

    # Failure applies penalty
    tracker.record_latency("MEAS:VOLT?", 10.0, success=False)
    # 0.5 * 500 + 0.5 * 20 = 260.0
    assert tracker.get_latency("MEAS:VOLT?") == 260.0


def test_solve_weighted_set_cover_exact() -> None:
    tracker = LatencyTracker()
    cand_composite = QueryDescriptor(
        command="MEAS:ALL:INFO?",
        fields=frozenset({FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER}),
        default_latency_ms=15.0,
    )
    cand_v = QueryDescriptor(
        command="MEAS:VOLT?",
        fields=frozenset({FIELD_VOLTAGE}),
        default_latency_ms=10.0,
    )
    cand_i = QueryDescriptor(
        command="MEAS:CURR?",
        fields=frozenset({FIELD_CURRENT}),
        default_latency_ms=10.0,
    )

    # When all 3 fields are needed:
    # composite cost = 15.0; discrete cost = 10.0 + 10.0 = 20.0 -> composite wins
    res = solve_weighted_set_cover(
        {FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER},
        [cand_composite, cand_v, cand_i],
        tracker,
    )
    assert len(res) == 1
    assert res[0].command == "MEAS:ALL:INFO?"

    # When only voltage is needed:
    # composite cost = 15.0; cand_v cost = 10.0 -> cand_v wins
    res_v = solve_weighted_set_cover(
        {FIELD_VOLTAGE},
        [cand_composite, cand_v, cand_i],
        tracker,
    )
    assert len(res_v) == 1
    assert res_v[0].command == "MEAS:VOLT?"


def test_query_planner_owon_and_standard_profiles() -> None:
    owon_planner = QueryPlanner(OwonScpiProfile(), channels=1, has_dmm=True)
    plan = owon_planner.plan_queries({FIELD_VOLTAGE, FIELD_CURRENT, FIELD_POWER, FIELD_STATUS_TEXT})
    assert len(plan) == 1
    assert plan[0].command == "MEAS:ALL:INFO?"

    std_planner = QueryPlanner(StandardScpiProfile(), channels=1, has_dmm=False)
    plan_std = std_planner.plan_queries({FIELD_VOLTAGE, FIELD_CURRENT, FIELD_OUTPUT_ENABLED})
    commands = {q.command for q in plan_std}
    assert "MEAS:VOLT?" in commands
    assert "MEAS:CURR?" in commands
    assert "OUTP?" in commands


def test_query_planner_dynamically_routes_around_slow_queries() -> None:
    owon_planner = QueryPlanner(OwonScpiProfile(), channels=1, has_dmm=False)
    # Artificially penalize MEAS:ALL:INFO? (e.g. repeated timeouts)
    owon_planner.latency_tracker.record_latency("MEAS:ALL:INFO?", 600.0, success=False)
    assert owon_planner.latency_tracker.get_latency("MEAS:ALL:INFO?") == 500.0

    # With composite penalized to 500ms, discrete queries (12ms + 12ms = 24ms) become faster
    plan = owon_planner.plan_queries({FIELD_VOLTAGE, FIELD_CURRENT})
    commands = [q.command for q in plan]
    assert "MEAS:ALL:INFO?" not in commands
    assert "MEAS:VOLT?" in commands
    assert "MEAS:CURR?" in commands


async def test_partial_telemetry_and_channel_field_interest() -> None:
    """Verify polling only a subset of fields (e.g. voltage disabled or channel 2 output only)."""
    from unittest.mock import patch

    from custom_components.scpi_psu.scpi_psu_client.client import ScpiPsuClient

    client = ScpiPsuClient(
        "socket://127.0.0.1:5025", initial_idn="Rigol Technologies,DP832,123,1.0"
    )
    queried_commands: list[str] = []

    def _responder(cmd: str, expect_reply: bool = True) -> str | None:
        queried_commands.append(cmd)
        if not expect_reply:
            return ""
        if cmd == "MEAS:CURR?":
            return "1.75"
        if "OUTP?" in cmd:
            return "ON"
        return None

    with patch.object(client, "_query_unlocked", side_effect=_responder):
        # 1. Poll only current (voltage sensor disabled by user) -> succeeds with voltage=None
        snap_curr = await client.poll_telemetry(channel=1, needed_fields={FIELD_CURRENT})
        assert snap_curr is not None
        assert snap_curr.current == 1.75
        assert snap_curr.voltage is None
        assert snap_curr.power is None
        assert "MEAS:VOLT?" not in queried_commands
        assert "OUTP?" not in queried_commands

        # 2. Poll only status_text on Standard SCPI -> covered by OUTP? without needing voltage/current
        queried_commands.clear()
        snap_status = await client.poll_telemetry(channel=1, needed_fields={FIELD_STATUS_TEXT})
        assert snap_status is not None
        assert snap_status.output_enabled is True
        assert snap_status.status_text == "Active"
        assert "OUTP?" in queried_commands
        assert "MEAS:VOLT?" not in queried_commands

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
