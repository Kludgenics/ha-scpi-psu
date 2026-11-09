"""SCPI dialect profiles and formatting."""

from __future__ import annotations

import contextlib
from typing import Protocol

from .models import PROFILE_OWON, PROFILE_STANDARD, TelemetrySnapshot, parse_strict_float


def parse_output_state(raw: str | None) -> bool | None:
    """Parse SCPI output state ('ON', '1', 'OFF', '0')."""
    if raw is None:
        return None
    token = raw.strip().upper()
    if token in ("ON", "1"):
        return True
    if token in ("OFF", "0"):
        return False
    return None


class ScpiProfile(Protocol):
    """Protocol for SCPI PSU dialect implementations."""

    key: str
    cmd_get_voltage_setpoint: str
    cmd_get_current_setpoint: str
    cmd_get_voltage_limit: str
    cmd_get_current_limit: str
    cmd_get_ovp_tripped: str | None
    cmd_get_ocp_tripped: str | None
    cmd_get_dmm_reading: str
    cmd_get_composite_telemetry: str | None
    reports_status_in_telemetry: bool
    supports_compound_queries: bool

    def format_set_voltage(self, value: float) -> str: ...
    def format_set_current(self, value: float) -> str: ...
    def format_set_voltage_limit(self, value: float) -> str: ...
    def format_set_current_limit(self, value: float) -> str: ...
    def format_set_output(self, enabled: bool, channel: int | None = None) -> str: ...
    def format_set_all_outputs(self, enabled: bool) -> str: ...
    def format_get_output(self, channel: int | None = None) -> str: ...
    def format_select_channel(self, channel: int) -> str: ...
    def parse_composite_telemetry(self, line: str | None) -> TelemetrySnapshot | None: ...


class BaseScpiProfile:
    """Shared IEEE 488.2 / SCPI command formatting."""

    cmd_get_voltage_setpoint = "VOLT?"
    cmd_get_current_setpoint = "CURR?"

    @staticmethod
    def format_set_voltage(value: float) -> str:
        return f"VOLT {value:.3f}"

    @staticmethod
    def format_set_current(value: float) -> str:
        return f"CURR {value:.3f}"

    @staticmethod
    def format_set_output(enabled: bool, channel: int | None = None) -> str:
        state = "ON" if enabled else "OFF"
        if channel is not None and channel > 1:
            return f"OUTP CH{channel},{state}"
        return f"OUTP {state}"

    @staticmethod
    def format_set_all_outputs(enabled: bool) -> str:
        state = "ON" if enabled else "OFF"
        return f"OUTP:ALL {state}"

    @staticmethod
    def format_get_output(channel: int | None = None) -> str:
        if channel is not None and channel > 1:
            return f"OUTP? CH{channel}"
        return "OUTP?"


class OwonScpiProfile(BaseScpiProfile):
    """OWON / Abestop / Kiprim SCPI dialect (MEAS:ALL:INFO?, VOLT:LIM, CURR:LIM)."""

    key = PROFILE_OWON
    cmd_get_voltage_limit = "VOLT:LIM?"
    cmd_get_current_limit = "CURR:LIM?"
    cmd_get_ovp_tripped: str | None = None
    cmd_get_ocp_tripped: str | None = None
    cmd_get_dmm_reading = ":READ?"
    cmd_get_composite_telemetry: str | None = "MEAS:ALL:INFO?"
    reports_status_in_telemetry = True
    supports_compound_queries = False

    @staticmethod
    def format_set_voltage_limit(value: float) -> str:
        return f"VOLT:LIM {value:.3f}"

    @staticmethod
    def format_set_current_limit(value: float) -> str:
        return f"CURR:LIM {value:.3f}"

    @staticmethod
    def format_select_channel(channel: int) -> str:
        return f"INST CH{channel}"

    @staticmethod
    def parse_composite_telemetry(line: str | None) -> TelemetrySnapshot | None:
        if not line:
            return None
        tokens = [t.strip() for t in line.split(",")]
        if len(tokens) < 6:
            return None

        v = parse_strict_float(tokens[0])
        i = parse_strict_float(tokens[1])
        p = parse_strict_float(tokens[2])
        if v is None or i is None or p is None:
            return None
        if not (0.0 <= v <= 200.0 and 0.0 <= i <= 100.0 and 0.0 <= p <= 5000.0):
            return None

        ovp = parse_output_state(tokens[3])
        ocp = parse_output_state(tokens[4])
        opp = parse_output_state(tokens[5])
        if ovp is None or ocp is None:
            return None

        code: int | None = None
        if len(tokens) >= 7:
            with contextlib.suppress(ValueError):
                code = int(float(tokens[6]))

        if code == 0:
            status_text = "Standby / Output Off"
        elif code == 1:
            status_text = "Constant Voltage (CV)"
        elif code == 2:
            status_text = "Constant Current (CC)"
        elif code == 3:
            status_text = "Fault / Protection Trip"
        else:
            status_text = "Active"

        fault = bool(code == 3 or ovp or ocp or opp)
        outp: bool | None = None
        if code in (1, 2):
            outp = True
        elif code == 0:
            outp = False

        return TelemetrySnapshot(
            voltage=round(v, 3),
            current=round(i, 3),
            power=round(p, 3),
            ovp_tripped=ovp,
            ocp_tripped=ocp,
            opp_tripped=bool(opp),
            status_text=status_text,
            fault_alarm=fault,
            output_enabled=outp,
        )


class StandardScpiProfile(BaseScpiProfile):
    """Standard IEEE 488.2 / SCPI-1999 PSU dialect (MEAS:VOLT?, VOLT:PROT, CURR:PROT)."""

    key = PROFILE_STANDARD
    cmd_get_voltage_limit = "VOLT:PROT?"
    cmd_get_current_limit = "CURR:PROT?"
    cmd_get_ovp_tripped: str | None = "VOLT:PROT:TRIP?"
    cmd_get_ocp_tripped: str | None = "CURR:PROT:TRIP?"
    cmd_get_dmm_reading = "MEAS:VOLT:DC?"
    cmd_get_composite_telemetry: str | None = None
    reports_status_in_telemetry = False
    supports_compound_queries = True

    @staticmethod
    def format_set_voltage_limit(value: float) -> str:
        return f"VOLT:PROT {value:.3f}"

    @staticmethod
    def format_set_current_limit(value: float) -> str:
        return f"CURR:PROT {value:.3f}"

    @staticmethod
    def format_select_channel(channel: int) -> str:
        return f"INST:NSEL {channel}"

    @staticmethod
    def parse_composite_telemetry(line: str | None) -> TelemetrySnapshot | None:
        return None


PROFILES: dict[str, OwonScpiProfile | StandardScpiProfile] = {
    PROFILE_OWON: OwonScpiProfile(),
    PROFILE_STANDARD: StandardScpiProfile(),
}
