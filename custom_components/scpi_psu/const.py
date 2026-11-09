"""Constants for the SCPI Programmable Power Supply integration."""

DOMAIN = "scpi_psu"

CONF_NAME = "name"
CONF_SERIAL_PORT = "serial_port"
CONF_BAUDRATE = "baudrate"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MAX_VOLTAGE_CLAMP = "max_voltage_clamp"
CONF_MAX_CURRENT_CLAMP = "max_current_clamp"
CONF_PROFILE = "profile"
CONF_IDN = "idn"

DEFAULT_BAUDRATE = 115200
DEFAULT_SCAN_INTERVAL = 2

PROFILE_AUTO = "auto"
PROFILE_OWON = "owon"
PROFILE_STANDARD = "standard"


def channel_identifier(entry_id: str, channel: int) -> str:
    """Format child device identifier for a specific channel."""
    return f"{entry_id}_ch{channel}"


def dmm_identifier(entry_id: str) -> str:
    """Format child device identifier for the built-in multimeter."""
    return f"{entry_id}_dmm"
