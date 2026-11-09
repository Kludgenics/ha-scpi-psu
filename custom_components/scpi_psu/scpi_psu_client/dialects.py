"""SCPI dialect profiles and formatting."""

from __future__ import annotations

import contextlib
from typing import Protocol

from .models import TelemetrySnapshot, parse_strict_float


class ScpiProfile(Protocol):
    """Protocol for SCPI PSU dialect implementations."""

    key: str
    cmd_get_voltage_setpoint: str
    cmd_get_current_setpoint: str
    cmd_get_voltage_limit: str
    cmd_get_current_limit: str

    def format_set_voltage(self, value: float) -> str: ...
    def format_set_current(self, value: float) -> str: ...
    def format_set_voltage_limit(self, value: float) -> str: ...
    def format_set_current_limit(self, value: float) -> str: ...
    def format_set_output(self, enabled: bool) -> str: ...


class OwonScpiProfile:
    """OWON / Abestop / Kiprim SCPI dialect (MEAS:ALL:INFO?, VOLT:LIM, CURR:LIM)."""

    key = "owon"
    cmd_get_voltage_setpoint = "VOLT?"
    cmd_get_current_setpoint = "CURR?"
    cmd_get_voltage_limit = "VOLT:LIM?"
    cmd_get_current_limit = "CURR:LIM?"

    @staticmethod
    def format_set_voltage(value: float) -> str:
        return f"VOLT {value:.3f}"

    @staticmethod
    def format_set_current(value: float) -> str:
        return f"CURR {value:.3f}"

    @staticmethod
    def format_set_voltage_limit(value: float) -> str:
        return f"VOLT:LIM {value:.3f}"

    @staticmethod
    def format_set_current_limit(value: float) -> str:
        return f"CURR:LIM {value:.3f}"

    @staticmethod
    def format_set_output(enabled: bool) -> str:
        return "OUTP ON" if enabled else "OUTP OFF"

    @staticmethod
    def parse_meas_all_info(line: str | None) -> TelemetrySnapshot | None:
        """Parse OWON composite MEAS:ALL:INFO? response."""
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

        ovp_raw = tokens[3].upper()
        ocp_raw = tokens[4].upper()
        opp_raw = tokens[5].upper()
        if ovp_raw not in ("ON", "OFF", "0", "1") or ocp_raw not in ("ON", "OFF", "0", "1"):
            return None

        ovp = ovp_raw in ("ON", "1")
        ocp = ocp_raw in ("ON", "1")
        opp = opp_raw in ("ON", "1")

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
            opp_tripped=opp,
            status_code=code,
            status_text=status_text,
            fault_alarm=fault,
            output_enabled=outp,
        )


class StandardScpiProfile:
    """Standard IEEE 488.2 / SCPI-1999 PSU dialect (MEAS:VOLT?, VOLT:PROT, CURR:PROT)."""

    key = "standard"
    cmd_get_voltage_setpoint = "VOLT?"
    cmd_get_current_setpoint = "CURR?"
    cmd_get_voltage_limit = "VOLT:PROT?"
    cmd_get_current_limit = "CURR:PROT?"

    @staticmethod
    def format_set_voltage(value: float) -> str:
        return f"VOLT {value:.3f}"

    @staticmethod
    def format_set_current(value: float) -> str:
        return f"CURR {value:.3f}"

    @staticmethod
    def format_set_voltage_limit(value: float) -> str:
        return f"VOLT:PROT {value:.3f}"

    @staticmethod
    def format_set_current_limit(value: float) -> str:
        return f"CURR:PROT {value:.3f}"

    @staticmethod
    def format_set_output(enabled: bool) -> str:
        return "OUTP ON" if enabled else "OUTP OFF"


PROFILES: dict[str, OwonScpiProfile | StandardScpiProfile] = {
    "owon": OwonScpiProfile(),
    "standard": StandardScpiProfile(),
}
