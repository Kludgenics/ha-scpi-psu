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
    "KNOWN_MODEL_SPECS",
    "PROFILES",
    "ChannelTelemetry",
    "OwonScpiProfile",
    "PsuIdentity",
    "ScpiProfile",
    "ScpiPsuClient",
    "StandardScpiProfile",
    "TelemetrySnapshot",
    "parse_idn",
    "parse_strict_float",
]
