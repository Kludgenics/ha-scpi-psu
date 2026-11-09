"""Tests for SCPI PSU config flow."""

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scpi_psu.config_flow import ScpiPsuConfigFlow, ScpiPsuOptionsFlow
from custom_components.scpi_psu.const import (
    CONF_BAUDRATE,
    CONF_SERIAL_PORT,
    DOMAIN,
)
from custom_components.scpi_psu.scpi_psu_client.models import PsuIdentity


async def test_flow_user_step_success(hass: HomeAssistant):
    flow = ScpiPsuConfigFlow()
    flow.hass = hass
    flow.context = {}

    result = await flow.async_step_user()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    ident = PsuIdentity(
        manufacturer="OWON",
        model="SPE6103",
        serial_number="2138123",
        firmware="V1.2.3",
        raw_idn="OWON,SPE6103,2138123,V1.2.3",
        profile_key="owon",
        hw_max_voltage=60.0,
        hw_max_current=10.0,
    )

    with patch("custom_components.scpi_psu.config_flow._async_probe_psu", return_value=ident):
        result2 = await flow.async_step_user(
            {
                CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
                CONF_BAUDRATE: 115200,
            }
        )
        assert result2["type"] is FlowResultType.FORM
        assert result2["step_id"] == "confirm"

        result3 = await flow.async_step_confirm({})
        assert result3["type"] is FlowResultType.CREATE_ENTRY
        assert result3["title"] == "OWON SPE6103"
        assert result3["data"][CONF_SERIAL_PORT] == "socket://127.0.0.1:5025"


async def test_options_flow(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025", CONF_BAUDRATE: 115200},
        entry_id="test_opts_entry",
    )
    flow = ScpiPsuOptionsFlow(entry)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
