"""Direct deterministic query builder for SCPI telemetry."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
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

COMPOSITE_FIELDS = frozenset(
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
)


@dataclass(frozen=True)
class QueryDescriptor:
    """Describes an SCPI query, the measurement fields it yields, and parser."""

    command: str
    fields: frozenset[str]
    parser: Callable[[str | None], dict[str, Any]] | None = None


class QueryPlanner:
    """Constructs minimal, deterministic SCPI telemetry queries."""

    def __init__(self, profile: ScpiProfile, has_dmm: bool = False) -> None:
        self.profile = profile
        self.has_dmm = has_dmm

    def plan_queries(
        self,
        needed_fields: Collection[str],
        channel: int = 1,
    ) -> list[QueryDescriptor]:
        """Build the single query or compound query sequence covering needed_fields."""
        fields = set(needed_fields)
        if not fields:
            return []

        prof = self.profile

        # 1. OWON / composite profile: use MEAS:ALL:INFO? if available and any composite field needed
        if prof.cmd_get_composite_telemetry and (fields & COMPOSITE_FIELDS):

            def _parse_composite(raw: str | None) -> dict[str, Any]:
                snap = prof.parse_composite_telemetry(raw)
                if snap is None:
                    return {}
                return {f: getattr(snap, f) for f in COMPOSITE_FIELDS}

            desc = QueryDescriptor(
                command=prof.cmd_get_composite_telemetry,
                fields=COMPOSITE_FIELDS,
                parser=_parse_composite,
            )
            # If DMM is also needed on this poll, DMM is a separate query
            if FIELD_DMM in fields and self.has_dmm and prof.cmd_get_dmm_reading:
                return [desc, self._build_dmm_descriptor()]
            return [desc]

        # 2. Discrete scalar queries
        descs: list[QueryDescriptor] = []

        if FIELD_VOLTAGE in fields:
            descs.append(
                QueryDescriptor(
                    command="MEAS:VOLT?",
                    fields=frozenset({FIELD_VOLTAGE}),
                    parser=lambda r: (
                        {FIELD_VOLTAGE: v}
                        if (v := parse_strict_float(r)) is not None and 0.0 <= v <= 500.0
                        else {}
                    ),
                )
            )

        if FIELD_CURRENT in fields:
            descs.append(
                QueryDescriptor(
                    command="MEAS:CURR?",
                    fields=frozenset({FIELD_CURRENT}),
                    parser=lambda r: (
                        {FIELD_CURRENT: i}
                        if (i := parse_strict_float(r)) is not None and 0.0 <= i <= 200.0
                        else {}
                    ),
                )
            )

        if FIELD_POWER in fields:
            descs.append(
                QueryDescriptor(
                    command="MEAS:POW?",
                    fields=frozenset({FIELD_POWER}),
                    parser=lambda r: (
                        {FIELD_POWER: p} if (p := parse_strict_float(r)) is not None else {}
                    ),
                )
            )

        if FIELD_OUTPUT_ENABLED in fields or (
            FIELD_STATUS_TEXT in fields and not prof.reports_status_in_telemetry
        ):
            outp_cmd = prof.format_get_output(channel)

            def _parse_outp(raw: str | None) -> dict[str, Any]:
                st = parse_output_state(raw)
                if st is None:
                    return {}
                res: dict[str, Any] = {FIELD_OUTPUT_ENABLED: st}
                if not prof.reports_status_in_telemetry:
                    res[FIELD_STATUS_TEXT] = "Active" if st else "Standby / Output Off"
                return res

            descs.append(
                QueryDescriptor(
                    command=outp_cmd,
                    fields=frozenset({FIELD_OUTPUT_ENABLED, FIELD_STATUS_TEXT}),
                    parser=_parse_outp,
                )
            )

        if FIELD_DMM in fields and self.has_dmm and prof.cmd_get_dmm_reading:
            descs.append(self._build_dmm_descriptor())

        if FIELD_OVP_TRIPPED in fields and prof.cmd_get_ovp_tripped:
            descs.append(
                QueryDescriptor(
                    command=prof.cmd_get_ovp_tripped,
                    fields=frozenset({FIELD_OVP_TRIPPED}),
                    parser=lambda r: (
                        {FIELD_OVP_TRIPPED: st} if (st := parse_output_state(r)) is not None else {}
                    ),
                )
            )

        if FIELD_OCP_TRIPPED in fields and prof.cmd_get_ocp_tripped:
            descs.append(
                QueryDescriptor(
                    command=prof.cmd_get_ocp_tripped,
                    fields=frozenset({FIELD_OCP_TRIPPED}),
                    parser=lambda r: (
                        {FIELD_OCP_TRIPPED: st} if (st := parse_output_state(r)) is not None else {}
                    ),
                )
            )

        if not descs:
            return []

        # 3. If dialect supports IEEE 488.2 compound queries, bundle into 1 round-trip!
        if prof.supports_compound_queries and len(descs) > 1:
            compound_cmd = ";:".join(d.command.lstrip(":") for d in descs)
            all_fields = frozenset().union(*(d.fields for d in descs))

            def _parse_compound(raw: str | None) -> dict[str, Any]:
                if not raw:
                    return {}
                parts = [p.strip() for p in raw.split(";")]
                if len(parts) != len(descs):
                    return {}
                res: dict[str, Any] = {}
                for desc, part in zip(descs, parts, strict=True):
                    if desc.parser:
                        res.update(desc.parser(part))
                return res

            return [
                QueryDescriptor(
                    command=compound_cmd,
                    fields=all_fields,
                    parser=_parse_compound,
                )
            ]

        return descs

    def _build_dmm_descriptor(self) -> QueryDescriptor:
        return QueryDescriptor(
            command=self.profile.cmd_get_dmm_reading,
            fields=frozenset({FIELD_DMM}),
            parser=lambda r: {FIELD_DMM: v} if (v := parse_strict_float(r)) is not None else {},
        )
