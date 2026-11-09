# Antigravity & Agent Guidelines for ha-scpi-psu

## Overview
`ha-scpi-psu` is a Home Assistant integration and pure-Python async client library for IEEE 488.2 / SCPI programmable bench DC power supplies (supporting standard SCPI as well as OWON, Abestop, Kiprim, and Multicomp dialects over serial, USB-serial, and networked serial `socket://` / `rfc2217://` URIs via `serialx`).

The repository is structured with two clear layers:
1. **`custom_components/scpi_psu`**: The Home Assistant integration component (`DataUpdateCoordinator`, entity platforms: sensor, binary_sensor, switch, number, button, and `config_flow`). Targets **Home Assistant 2026.11+**.
2. **`packages/scpi_psu_client`**: The pure-Python standalone library `scpi-psu` (packaged for PyPI) containing protocol framing, SCPI dialect abstraction, and hardware models. (A vendored copy is maintained in `custom_components/scpi_psu/scpi_psu_client` for self-contained HACS installation).

---

## Architecture & Design Rules

### 1. Separation of Concerns (Transport vs Coordinator)
- **Transport & Protocol**: All raw serial I/O, lock management, SCPI command encoding/parsing, and IEEE 488.2 dialect differences belong inside `scpi_psu_client.client.ScpiPsuClient` and `dialects.py`.
- **Home Assistant Glue**: `ScpiPsuCoordinator` must **never** manage raw serial sockets or duplicate SCPI framing. It instantiates `self.client = ScpiPsuClient(...)` and translates telemetry into HA state updates.
- **Config Flow Probing**: `config_flow.py` probes devices via `client.probe_identity()`, avoiding ad-hoc serial reader loops.

### 2. Single Source of Truth for Library Code
- `packages/scpi_psu_client/scpi_psu_client` is a git symlink pointing directly to `custom_components/scpi_psu/scpi_psu_client`.
- There is only **one** copy of the client code to edit and maintain. It works natively as part of the HACS custom component while simultaneously building and testing as the standalone `scpi-psu` package.

---

## Development & Test Execution

### Environment Setup
- Python: **3.14+** (recommended for Home Assistant 2026.11+ compatibility) or 3.12+.
- Package management with `uv`:
  ```bash
  uv venv .venv --python 3.14
  uv pip install -e packages/scpi_psu_client pytest pytest-asyncio pytest-homeassistant-custom-component pyserial aiousbwatcher
  ```

### Running Tests
The test configuration is anchored in `pyproject.toml` with `asyncio_mode = "auto"`:
```bash
.venv/bin/pytest tests
```
Ensure all tests pass before making any releases or commits.
