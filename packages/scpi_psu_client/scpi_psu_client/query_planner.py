"""Direct deterministic query builder for SCPI telemetry."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from .dialects import STATUS_ACTIVE, STATUS_STANDBY, ScpiProfile, parse_output_state
from .models import parse_strict_float

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


def compound_command(commands: Sequence[str]) -> str:
    """Join SCPI commands with absolute headers for a compound query."""
    return ";:".join(command.lstrip(":") for command in commands)


def split_compound_response(raw: str | None, count: int) -> list[str] | None:
    """Return one response per query, or None for an incomplete response."""
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(";")]
    return parts if len(parts) == count else None


@dataclass(frozen=True)
class QueryDescriptor:
    """Describes an SCPI query, the measurement fields it yields, and parser."""

    command: str
    fields: frozenset[str]
    parser: Callable[[str | None], dict[str, Any]] | None = None
    component_count: int = 1


def _float_descriptor(command: str, field: str, max_val: float | None = None) -> QueryDescriptor:
    def _parse(raw: str | None) -> dict[str, Any]:
        val = parse_strict_float(raw)
        if val is None:
            return {}
        if max_val is not None and not (0.0 <= val <= max_val):
            return {}
        return {field: val}

    return QueryDescriptor(command=command, fields=frozenset({field}), parser=_parse)


def _bool_descriptor(command: str, field: str) -> QueryDescriptor:
    return QueryDescriptor(
        command=command,
        fields=frozenset({field}),
        parser=lambda r: {field: st} if (st := parse_output_state(r)) is not None else {},
    )


class QueryPlanner:
    """Constructs minimal, deterministic SCPI telemetry queries."""

    def __init__(
        self, profile: ScpiProfile, has_dmm: bool = False, *, compound_queries: bool = True
    ) -> None:
        self.profile = profile
        self.has_dmm = has_dmm
        self.compound_queries = compound_queries

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
        descs: list[QueryDescriptor] = []

        if prof.cmd_get_composite_telemetry and (fields & COMPOSITE_FIELDS):

            def _parse_composite(raw: str | None) -> dict[str, Any]:
                snap = prof.parse_composite_telemetry(raw)
                if snap is None:
                    return {}
                return {f: getattr(snap, f) for f in COMPOSITE_FIELDS}

            descs.append(
                QueryDescriptor(
                    command=prof.cmd_get_composite_telemetry,
                    fields=COMPOSITE_FIELDS,
                    parser=_parse_composite,
                )
            )
        else:
            if FIELD_VOLTAGE in fields:
                descs.append(_float_descriptor("MEAS:VOLT?", FIELD_VOLTAGE, max_val=500.0))
            if FIELD_CURRENT in fields:
                descs.append(_float_descriptor("MEAS:CURR?", FIELD_CURRENT, max_val=200.0))
            if FIELD_POWER in fields:
                descs.append(_float_descriptor("MEAS:POW?", FIELD_POWER))

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
                        res[FIELD_STATUS_TEXT] = STATUS_ACTIVE if st else STATUS_STANDBY
                    return res

                descs.append(
                    QueryDescriptor(
                        command=outp_cmd,
                        fields=frozenset({FIELD_OUTPUT_ENABLED, FIELD_STATUS_TEXT}),
                        parser=_parse_outp,
                    )
                )

            if FIELD_OVP_TRIPPED in fields and prof.cmd_get_ovp_tripped:
                descs.append(_bool_descriptor(prof.cmd_get_ovp_tripped, FIELD_OVP_TRIPPED))
            if FIELD_OCP_TRIPPED in fields and prof.cmd_get_ocp_tripped:
                descs.append(_bool_descriptor(prof.cmd_get_ocp_tripped, FIELD_OCP_TRIPPED))

        if FIELD_DMM in fields and self.has_dmm and prof.cmd_get_dmm_reading:
            descs.append(_float_descriptor(prof.cmd_get_dmm_reading, FIELD_DMM))

        if not descs:
            return []

        if self.compound_queries and prof.supports_compound_queries and len(descs) > 1:
            compound_cmd = compound_command([d.command for d in descs])
            all_fields = frozenset().union(*(d.fields for d in descs))

            def _parse_compound(raw: str | None) -> dict[str, Any]:
                parts = split_compound_response(raw, len(descs))
                if parts is None:
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
                    component_count=len(descs),
                )
            ]

        return descs
