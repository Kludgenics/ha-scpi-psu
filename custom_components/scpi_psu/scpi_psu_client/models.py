"""Data models and capabilities for SCPI power supplies."""

from __future__ import annotations

from dataclasses import dataclass


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
    channels: int = 1
    has_dmm: bool = False

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
    fault_alarm: bool | None = False
    output_enabled: bool | None = None
    dmm_reading: float | None = None
    channel: int = 1


# Known model hardware ratings: (max_voltage, max_current, channels)
KNOWN_MODEL_SPECS: dict[str, tuple[float, float, int]] = {
    "AT6301": (60.0, 10.0, 1),
    "SPE6103": (60.0, 10.0, 1),
    "SPE6053": (60.0, 5.0, 1),
    "SPE3103": (30.0, 10.0, 1),
    "SPE3051": (30.0, 5.0, 1),
    "SPM6103": (60.0, 10.0, 1),
    "SPM3103": (30.0, 10.0, 1),
    "P4603": (60.0, 3.0, 1),
    "P4305": (30.0, 5.0, 1),
    "ODP6062": (60.0, 6.0, 2),
    "SPD3303X": (32.0, 3.2, 3),
    "SPD3303X-E": (32.0, 3.2, 3),
    "SPD1168X": (16.0, 8.0, 1),
    "SPD1305X": (30.0, 5.0, 1),
    "DP832": (30.0, 3.0, 3),
    "DP832A": (30.0, 3.0, 3),
    "DP831A": (30.0, 5.0, 3),
    "DP711": (30.0, 5.0, 1),
    "DP712": (50.0, 3.0, 1),
    "E36311A": (25.0, 5.0, 3),
    "E36103B": (20.0, 5.0, 1),
    "E36105B": (60.0, 2.0, 1),
    "HMP4040": (32.0, 10.0, 4),
    "HMP2020": (32.0, 10.0, 2),
    "KA3005P": (30.0, 5.0, 1),
    "KD3005P": (30.0, 5.0, 1),
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
            channels=1,
            has_dmm=False,
        )

    parts = [p.strip() for p in raw_idn.split(",")]
    manufacturer = parts[0] if len(parts) >= 1 and parts[0] else "SCPI"
    model = parts[1] if len(parts) >= 2 and parts[1] else "PSU"
    serial_number = (
        parts[2] if len(parts) >= 3 and parts[2] and parts[2] not in ("0", "N/A", "NONE") else None
    )
    firmware = parts[3] if len(parts) >= 4 and parts[3] else None

    mfr_lower = manufacturer.lower()
    model_upper = model.upper()

    is_owon = any(v in mfr_lower for v in OWON_VENDORS) or any(
        model_upper.startswith(p) for p in OWON_MODEL_PREFIXES
    )
    has_dmm = model_upper.startswith("SPM")

    profile_key = "owon" if is_owon else "standard"

    hw_v, hw_i, channels = KNOWN_MODEL_SPECS.get(model_upper, (60.0, 10.0, 1))

    return PsuIdentity(
        manufacturer=manufacturer,
        model=model,
        serial_number=serial_number,
        firmware=firmware,
        raw_idn=raw_idn,
        profile_key=profile_key,
        hw_max_voltage=hw_v,
        hw_max_current=hw_i,
        channels=channels,
        has_dmm=has_dmm,
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
