"""Data models and capabilities for SCPI power supplies."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields

DEFAULT_BAUDRATE = 115200
DEFAULT_HW_SPECS: tuple[float, float, int] = (60.0, 10.0, 1)
PROFILE_OWON = "owon"
PROFILE_STANDARD = "standard"
STATUS_ACTIVE = "Active"
STATUS_STANDBY = "Standby / Output Off"


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
    def is_valid(self) -> bool:
        """Whether identity discovery supplied a structured IDN response."""
        return "," in self.raw_idn

    @property
    def default_title(self) -> str:
        """Default device title based on discovered hardware."""
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model}"
        return self.model or "SCPI Power Supply"


@dataclass
class ChannelTelemetry:
    """Telemetry for a single channel."""

    channel: int = 1
    voltage: float | None = None
    current: float | None = None
    power: float | None = None
    output_enabled: bool | None = None
    ovp_tripped: bool | None = None
    ocp_tripped: bool | None = None
    opp_tripped: bool | None = None
    status_text: str | None = STATUS_ACTIVE
    fault_alarm: bool | None = None

    def update_snapshot(
        self, snap: TelemetrySnapshot | None, requested_fields: Collection[str] = ()
    ) -> None:
        """Replace requested readings, retaining unrequested values on partial polls."""
        self.clear_fields(requested_fields)
        if snap is not None:
            self.apply_snapshot(snap)

    def apply_snapshot(self, snap: TelemetrySnapshot) -> None:
        """Update non-None fields from a TelemetrySnapshot."""
        for f in dataclass_fields(self):
            if f.name != "channel":
                val = getattr(snap, f.name, None)
                if val is not None:
                    setattr(self, f.name, val)

    def clear_fields(self, field_names: Collection[str]) -> None:
        """Mark requested channel measurements unknown after a failed poll."""
        for f in dataclass_fields(self):
            if f.name != "channel" and f.name in field_names:
                setattr(self, f.name, None)


@dataclass
class TelemetrySnapshot:
    """Normalized telemetry returned by a profile poll."""

    voltage: float | None = None
    current: float | None = None
    power: float | None = None
    ovp_tripped: bool | None = None
    ocp_tripped: bool | None = None
    opp_tripped: bool | None = None
    status_text: str | None = None
    fault_alarm: bool | None = None
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
        hw_v, hw_i, channels = DEFAULT_HW_SPECS
        return PsuIdentity(
            manufacturer="SCPI",
            model="Power Supply",
            serial_number=None,
            firmware=None,
            raw_idn=raw_idn or "",
            profile_key=PROFILE_STANDARD,
            hw_max_voltage=hw_v,
            hw_max_current=hw_i,
            channels=channels,
            has_dmm=False,
        )

    parts = [p.strip() for p in raw_idn.split(",")]
    manufacturer = parts[0] or "SCPI"
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

    profile_key = PROFILE_OWON if is_owon else PROFILE_STANDARD

    hw_v, hw_i, channels = KNOWN_MODEL_SPECS.get(model_upper, DEFAULT_HW_SPECS)

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
