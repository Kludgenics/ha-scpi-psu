"""SCPI PSU client package."""

from __future__ import annotations

from .client import ScpiPsuClient
from .dialects import PROFILES, OwonScpiProfile, StandardScpiProfile
from .exceptions import (
    ScpiConnectionError,
    ScpiError,
    ScpiProtocolError,
    ScpiTimeoutError,
)
from .models import (
    KNOWN_MODEL_LIMITS,
    PsuIdentity,
    TelemetrySnapshot,
    parse_idn,
    parse_strict_float,
)

__all__ = [
    "KNOWN_MODEL_LIMITS",
    "PROFILES",
    "OwonScpiProfile",
    "PsuIdentity",
    "ScpiConnectionError",
    "ScpiError",
    "ScpiProtocolError",
    "ScpiPsuClient",
    "ScpiTimeoutError",
    "StandardScpiProfile",
    "TelemetrySnapshot",
    "parse_idn",
    "parse_strict_float",
]
