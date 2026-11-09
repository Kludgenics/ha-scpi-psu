import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure repo root is on python path
repo_root = Path(__file__).parent.parent.resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# Mock usb component SerialDevice / async_register_serial_port_scanner if missing from HA test env
try:
    import homeassistant.components.usb as ha_usb

    if not hasattr(ha_usb, "SerialDevice"):
        from dataclasses import dataclass

        @dataclass
        class SerialDevice:
            device: str
            serial_number: str | None = None
            manufacturer: str | None = None
            description: str | None = None

        ha_usb.SerialDevice = SerialDevice
    if not hasattr(ha_usb, "async_register_serial_port_scanner"):
        ha_usb.async_register_serial_port_scanner = MagicMock()
except ImportError:
    pass

pytest_plugins = ["pytest_homeassistant_custom_component"]
