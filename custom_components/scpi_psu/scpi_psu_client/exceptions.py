"""Exceptions for SCPI PSU communication."""

from __future__ import annotations


class ScpiError(Exception):
    """Base exception for all SCPI operations."""


class ScpiConnectionError(ScpiError):
    """Error connecting to the SCPI transport."""


class ScpiTimeoutError(ScpiError):
    """Timeout waiting for SCPI command execution or reply."""


class ScpiProtocolError(ScpiError):
    """Malformed or invalid SCPI response."""
