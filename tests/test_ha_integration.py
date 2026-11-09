from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu.const import (
    CONF_BAUDRATE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.scpi_psu.coordinator import ScpiPsuCoordinator
from custom_components.scpi_psu.scpi_psu_client.models import TelemetrySnapshot


@pytest.fixture
def mock_config_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        title="OWON SPE6103",
        data={
            CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
            CONF_BAUDRATE: 115200,
            CONF_SCAN_INTERVAL: 2,
        },
        entry_id="test_scpi_entry",
    )


async def test_coordinator_update(hass: HomeAssistant, mock_config_entry: MockConfigEntry):
    mock_config_entry.add_to_hass(hass)
    coordinator = ScpiPsuCoordinator(hass, mock_config_entry)

    snap = TelemetrySnapshot(
        voltage=12.5,
        current=1.2,
        power=15.0,
        output_enabled=True,
        status_text="Constant Voltage (CV)",
    )

    with (
        patch.object(coordinator.client, "poll_telemetry", new_callable=AsyncMock) as mock_poll,
        patch.object(coordinator.client, "get_output", new_callable=AsyncMock) as mock_outp,
        patch.object(coordinator.client, "get_raw_idn", new_callable=AsyncMock) as mock_idn,
        patch.object(
            coordinator.client, "get_config_registers", new_callable=AsyncMock
        ) as mock_regs,
        patch.object(coordinator.client, "get_system_error", new_callable=AsyncMock) as mock_err,
    ):
        mock_poll.return_value = snap
        mock_outp.return_value = True
        mock_idn.return_value = "OWON,SPE6103,1234,V1.0"
        mock_regs.return_value = {
            "voltage_limit": 60.0,
            "current_limit": 10.0,
            "voltage_setpoint": 12.5,
            "current_setpoint": 1.2,
        }
        mock_err.return_value = "0,No error"

        data = await coordinator._async_update_data()
        assert data.responding is True
        assert data.voltage == 12.5
        assert data.current == 1.2
        assert data.power == 15.0
        assert data.output_enabled is True
        assert data.voltage_setpoint == 12.5
