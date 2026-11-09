"""SCPI PSU client package."""

from __future__ import annotations

from .client import ScpiPsuClient
from .dialects import PROFILES, OwonScpiProfile, ScpiProfile, StandardScpiProfile
from .models import (
    KNOWN_MODEL_SPECS,
    ChannelTelemetry,
    PsuIdentity,
    TelemetrySnapshot,
    parse_idn,
    parse_strict_float,
)

__all__ = [
    "FIELD_CURRENT",
    "FIELD_DMM",
    "FIELD_FAULT_ALARM",
    "FIELD_OUTPUT_ENABLED",
    "FIELD_POWER",
    "FIELD_STATUS_TEXT",
    "FIELD_VOLTAGE",
    "KNOWN_MODEL_SPECS",
    "PROFILES",
    "ChannelTelemetry",
    "LatencyTracker",
    "OwonScpiProfile",
    "PsuIdentity",
    "QueryDescriptor",
    "QueryPlanner",
    "ScpiProfile",
    "ScpiPsuClient",
    "StandardScpiProfile",
    "TelemetrySnapshot",
    "parse_idn",
    "parse_strict_float",
]

from .query_planner import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_FAULT_ALARM,
    FIELD_OUTPUT_ENABLED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    LatencyTracker,
    QueryDescriptor,
    QueryPlanner,
)
