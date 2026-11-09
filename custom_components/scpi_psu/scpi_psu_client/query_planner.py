"""Weighted Set Cover Query Planner and Latency Tracker for SCPI telemetry.

Optimally resolves a minimal-latency set of SCPI queries to satisfy the
measurements required by enabled Home Assistant entities at each poll tick.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from .dialects import ScpiProfile, parse_output_state
from .models import parse_strict_float

# Canonical measurement fields produced by queries
FIELD_VOLTAGE = "voltage"
FIELD_CURRENT = "current"
FIELD_POWER = "power"
FIELD_OUTPUT_ENABLED = "output_enabled"
FIELD_STATUS_TEXT = "status_text"
FIELD_FAULT_ALARM = "fault_alarm"
FIELD_OVP_TRIPPED = "ovp_tripped"
FIELD_OCP_TRIPPED = "ocp_tripped"
FIELD_OPP_TRIPPED = "opp_tripped"
FIELD_DMM = "dmm_reading"


@dataclass(frozen=True)
class QueryDescriptor:
    """Describes an SCPI query, the measurement fields it yields, and parser."""

    command: str
    fields: frozenset[str]
    channel: int | None = None
    default_latency_ms: float = 20.0
    parser: Callable[[str | None], dict[str, Any]] | None = None


@dataclass
class LatencyTracker:
    """Exponentially weighted moving average (EWMA) tracker for query latencies."""

    alpha: float = 0.25  # Weight for new observations
    default_latency_ms: float = 20.0
    timeout_penalty_ms: float = 500.0
    _latencies: dict[str, float] = field(default_factory=dict)
    _sample_counts: dict[str, int] = field(default_factory=dict)

    def get_latency(self, command: str, fallback_ms: float = 20.0) -> float:
        """Return the current estimated latency in milliseconds for a command."""
        return self._latencies.get(command, fallback_ms)

    def record_latency(self, command: str, latency_ms: float, success: bool = True) -> None:
        """Record an observed execution latency."""
        effective_latency = latency_ms if success else self.timeout_penalty_ms
        current = self._latencies.get(command)
        if current is None:
            self._latencies[command] = effective_latency
            self._sample_counts[command] = 1
        else:
            self._latencies[command] = (self.alpha * effective_latency) + (
                (1.0 - self.alpha) * current
            )
            self._sample_counts[command] += 1

    def dump_stats(self) -> dict[str, dict[str, float | int]]:
        """Return latency stats for diagnostics."""
        return {
            cmd: {"latency_ms": round(lat, 2), "samples": self._sample_counts.get(cmd, 0)}
            for cmd, lat in self._latencies.items()
        }


def solve_weighted_set_cover(
    universe: set[str],
    candidates: Sequence[QueryDescriptor],
    latency_tracker: LatencyTracker,
) -> list[QueryDescriptor]:
    """Find the exact minimum-latency subset of candidates covering the universe.

    Uses bitmask branch-and-bound search. Because candidate set size for SCPI
    supplies is typically <= 15 commands, this runs deterministically in < 50 microseconds.
    """
    needed_fields = sorted(universe)
    if not needed_fields:
        return []

    field_to_bit = {f: 1 << i for i, f in enumerate(needed_fields)}
    target_mask = (1 << len(needed_fields)) - 1

    # Filter and precompute bitmasks & weights
    usable: list[tuple[QueryDescriptor, int, float]] = []
    for cand in candidates:
        cand_mask = 0
        for f in cand.fields:
            if f in field_to_bit:
                cand_mask |= field_to_bit[f]
        if cand_mask > 0:
            weight = latency_tracker.get_latency(cand.command, cand.default_latency_ms)
            usable.append((cand, cand_mask, weight))

    if not usable:
        return []

    # Sort descending by coverage / weight heuristic to improve branch-and-bound pruning
    usable.sort(key=lambda item: bin(item[1]).count("1") / max(item[2], 0.1), reverse=True)

    best_weight = float("inf")
    best_selection: list[QueryDescriptor] = []

    def search(
        index: int, current_mask: int, current_weight: float, path: list[QueryDescriptor]
    ) -> None:
        nonlocal best_weight, best_selection

        if current_mask == target_mask:
            if current_weight < best_weight:
                best_weight = current_weight
                best_selection = list(path)
            return

        if current_weight >= best_weight or index >= len(usable):
            return

        cand, c_mask, weight = usable[index]

        # Branch 1: Include cand (only if it contributes new fields)
        if (c_mask & ~current_mask) != 0:
            path.append(cand)
            search(index + 1, current_mask | c_mask, current_weight + weight, path)
            path.pop()

        # Branch 2: Exclude cand
        search(index + 1, current_mask, current_weight, path)

    search(0, 0, 0.0, [])
    return best_selection


class QueryPlanner:
    """Manages command candidate generation, latency tracking, and poll dispatch."""

    def __init__(self, profile: ScpiProfile, channels: int = 1, has_dmm: bool = False) -> None:
        self.profile = profile
        self.channels = max(1, channels)
        self.has_dmm = has_dmm
        self.latency_tracker = LatencyTracker()

    def build_candidates_for_channel(self, channel: int = 1) -> list[QueryDescriptor]:
        """Generate all candidate queries for a specific channel under current profile."""
        candidates: list[QueryDescriptor] = []
        prof = self.profile

        # Composite query (e.g. MEAS:ALL:INFO? on OWON/Abestop)
        if prof.cmd_get_composite_telemetry:

            def _parse_composite(raw: str | None) -> dict[str, Any]:
                snap = prof.parse_composite_telemetry(raw)
                if snap is None:
                    return {}
                return {
                    FIELD_VOLTAGE: snap.voltage,
                    FIELD_CURRENT: snap.current,
                    FIELD_POWER: snap.power,
                    FIELD_STATUS_TEXT: snap.status_text,
                    FIELD_FAULT_ALARM: snap.fault_alarm,
                    FIELD_OUTPUT_ENABLED: snap.output_enabled,
                    FIELD_OVP_TRIPPED: snap.ovp_tripped,
                    FIELD_OCP_TRIPPED: snap.ocp_tripped,
                    FIELD_OPP_TRIPPED: snap.opp_tripped,
                }

            candidates.append(
                QueryDescriptor(
                    command=prof.cmd_get_composite_telemetry,
                    fields=frozenset(
                        {
                            FIELD_VOLTAGE,
                            FIELD_CURRENT,
                            FIELD_POWER,
                            FIELD_STATUS_TEXT,
                            FIELD_FAULT_ALARM,
                            FIELD_OUTPUT_ENABLED,
                            FIELD_OVP_TRIPPED,
                            FIELD_OCP_TRIPPED,
                            FIELD_OPP_TRIPPED,
                        }
                    ),
                    channel=channel,
                    default_latency_ms=18.0,
                    parser=_parse_composite,
                )
            )

        # Standard discrete queries
        # Voltage
        def _parse_v(raw: str | None) -> dict[str, Any]:
            v = parse_strict_float(raw)
            return {FIELD_VOLTAGE: v} if (v is not None and 0.0 <= v <= 500.0) else {}

        candidates.append(
            QueryDescriptor(
                command="MEAS:VOLT?",
                fields=frozenset({FIELD_VOLTAGE}),
                channel=channel,
                default_latency_ms=12.0,
                parser=_parse_v,
            )
        )

        # Current
        def _parse_i(raw: str | None) -> dict[str, Any]:
            curr = parse_strict_float(raw)
            return {FIELD_CURRENT: curr} if (curr is not None and 0.0 <= curr <= 200.0) else {}

        candidates.append(
            QueryDescriptor(
                command="MEAS:CURR?",
                fields=frozenset({FIELD_CURRENT}),
                channel=channel,
                default_latency_ms=12.0,
                parser=_parse_i,
            )
        )

        # Power
        def _parse_p(raw: str | None) -> dict[str, Any]:
            p = parse_strict_float(raw)
            return {FIELD_POWER: p} if p is not None else {}

        candidates.append(
            QueryDescriptor(
                command="MEAS:POW?",
                fields=frozenset({FIELD_POWER}),
                channel=channel,
                default_latency_ms=12.0,
                parser=_parse_p,
            )
        )

        # Output state (also synthesizes status_text for dialects without composite telemetry)
        def _parse_outp(raw: str | None) -> dict[str, Any]:
            st = parse_output_state(raw)
            if st is None:
                return {}
            res: dict[str, Any] = {FIELD_OUTPUT_ENABLED: st}
            if not prof.reports_status_in_telemetry:
                res[FIELD_STATUS_TEXT] = "Active" if st else "Standby / Output Off"
            return res

        outp_fields = (
            frozenset({FIELD_OUTPUT_ENABLED})
            if prof.reports_status_in_telemetry
            else frozenset({FIELD_OUTPUT_ENABLED, FIELD_STATUS_TEXT})
        )
        outp_cmd = prof.format_get_output(channel) if self.channels > 1 else "OUTP?"
        candidates.append(
            QueryDescriptor(
                command=outp_cmd,
                fields=outp_fields,
                channel=channel,
                default_latency_ms=10.0,
                parser=_parse_outp,
            )
        )

        # DMM reading if available
        if self.has_dmm and prof.cmd_get_dmm_reading:

            def _parse_dmm(raw: str | None) -> dict[str, Any]:
                val = parse_strict_float(raw)
                return {FIELD_DMM: val} if val is not None else {}

            candidates.append(
                QueryDescriptor(
                    command=prof.cmd_get_dmm_reading,
                    fields=frozenset({FIELD_DMM}),
                    channel=channel,
                    default_latency_ms=25.0,
                    parser=_parse_dmm,
                )
            )

        return candidates

    def plan_queries(
        self,
        needed_fields: Collection[str],
        channel: int = 1,
    ) -> list[QueryDescriptor]:
        """Compute the optimal query sequence covering needed_fields with minimal latency."""
        candidates = self.build_candidates_for_channel(channel)
        all_producible = set().union(*(c.fields for c in candidates)) if candidates else set()
        target_universe = set(needed_fields) & all_producible
        return solve_weighted_set_cover(target_universe, candidates, self.latency_tracker)
