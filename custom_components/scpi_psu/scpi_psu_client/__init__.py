"""SCPI PSU client package."""

from __future__ import annotations

from .client import ScpiPsuClient
from .dialects import PROFILES, OwonScpiProfile, ScpiProfile, StandardScpiProfile
from .models import (
    DEFAULT_BAUDRATE,
    KNOWN_MODEL_SPECS,
    PROFILE_OWON,
    PROFILE_STANDARD,
    ChannelTelemetry,
    PsuIdentity,
    TelemetrySnapshot,
    parse_idn,
    parse_strict_float,
)
from .query_planner import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_FAULT_ALARM,
    FIELD_OCP_TRIPPED,
    FIELD_OPP_TRIPPED,
    FIELD_OUTPUT_ENABLED,
    FIELD_OVP_TRIPPED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    QueryDescriptor,
    QueryPlanner,
)

__all__ = [
    "DEFAULT_BAUDRATE",
    "FIELD_CURRENT",
    "FIELD_DMM",
    "FIELD_FAULT_ALARM",
    "FIELD_OCP_TRIPPED",
    "FIELD_OPP_TRIPPED",
    "FIELD_OUTPUT_ENABLED",
    "FIELD_OVP_TRIPPED",
    "FIELD_POWER",
    "FIELD_STATUS_TEXT",
    "FIELD_VOLTAGE",
    "KNOWN_MODEL_SPECS",
    "PROFILES",
    "PROFILE_OWON",
    "PROFILE_STANDARD",
    "ChannelTelemetry",
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
