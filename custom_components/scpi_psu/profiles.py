"""SCPI PSU dialect profiles and model capability detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PsuIdentity:
    """Parsed *IDN? identity and hardware limits for an SCPI PSU."""

    manufacturer: str
    model: str
    serial_number: str | None
    firmware: str | None
    raw_idn: str
    profile_key: str
    hw_max_voltage: float
    hw_max_current: float

    @property
    def default_title(self) -> str:
        """Default device title based on discovered hardware."""
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model}"
        return self.model or "SCPI Power Supply"


@dataclass
class TelemetrySnapshot:
    """Normalized telemetry returned by a profile poll."""

    voltage: float
    current: float
    power: float
    ovp_tripped: bool | None = None
    ocp_tripped: bool | None = None
    opp_tripped: bool | None = None
    status_code: int | None = None
    status_text: str = "Active"
    fault_alarm: bool = False
    output_enabled: bool | None = None


# Known model hardware ratings: (max_voltage, max_current)
KNOWN_MODEL_LIMITS: dict[str, tuple[float, float]] = {
    "AT6301": (60.0, 10.0),
    "SPE6103": (60.0, 10.0),
    "SPE6053": (60.0, 5.0),
    "SPE3103": (30.0, 10.0),
    "SPE3051": (30.0, 5.0),
    "SPM6103": (60.0, 10.0),
    "SPM3103": (30.0, 10.0),
    "P4603": (60.0, 3.0),
    "P4305": (30.0, 5.0),
    "ODP6062": (60.0, 6.0),
    "SPD3303X": (32.0, 3.2),
    "SPD3303X-E": (32.0, 3.2),
    "SPD1168X": (16.0, 8.0),
    "SPD1305X": (30.0, 5.0),
    "DP832": (30.0, 3.0),
    "DP832A": (30.0, 3.0),
    "DP711": (30.0, 5.0),
    "DP712": (50.0, 3.0),
    "E36311A": (25.0, 5.0),
    "E36103B": (20.0, 5.0),
    "E36105B": (60.0, 2.0),
}

OWON_VENDORS = ("owon", "abestop", "kiprim", "multicomp")
OWON_MODEL_PREFIXES = ("AT6", "SPE", "SPM", "P4", "ODP")


def parse_idn(raw_idn: str | None) -> PsuIdentity:
    """Parse an IEEE 488.2 *IDN? response and select the matching SCPI profile."""
    if not raw_idn or "," not in raw_idn:
        return PsuIdentity(
            manufacturer="SCPI",
            model="Power Supply",
            serial_number=None,
            firmware=None,
            raw_idn=raw_idn or "",
            profile_key="standard",
            hw_max_voltage=60.0,
            hw_max_current=10.0,
        )

    parts = [p.strip() for p in raw_idn.split(",")]
    manufacturer = parts[0] if len(parts) >= 1 and parts[0] else "SCPI"
    model = parts[1] if len(parts) >= 2 and parts[1] else "PSU"
    serial_number = (
        parts[2]
        if len(parts) >= 3 and parts[2] and parts[2] not in ("0", "N/A", "NONE")
        else None
    )
    firmware = parts[3] if len(parts) >= 4 and parts[3] else None

    mfr_lower = manufacturer.lower()
    model_upper = model.upper()

    is_owon = any(v in mfr_lower for v in OWON_VENDORS) or any(
        model_upper.startswith(p) for p in OWON_MODEL_PREFIXES
    )
    profile_key = "owon" if is_owon else "standard"

    hw_v, hw_i = KNOWN_MODEL_LIMITS.get(model_upper, (60.0, 10.0))

    return PsuIdentity(
        manufacturer=manufacturer,
        model=model,
        serial_number=serial_number,
        firmware=firmware,
        raw_idn=raw_idn,
        profile_key=profile_key,
        hw_max_voltage=hw_v,
        hw_max_current=hw_i,
    )


def parse_strict_float(token: str | None) -> float | None:
    """Parse a clean floating point value or return None."""
    if not token:
        return None
    s = token.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


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
            try:
                code = int(float(tokens[6]))
            except ValueError:
                pass

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
