"""Tests for SCPI PSU config flow and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from serialx import SerialException

from custom_components.scpi_psu.config_flow import (
    ScpiPsuConfigFlow,
    ScpiPsuOptionsFlow,
)
from custom_components.scpi_psu.const import (
    CONF_BAUDRATE,
    CONF_MAX_CURRENT_CLAMP,
    CONF_MAX_VOLTAGE_CLAMP,
    CONF_PROFILE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    DOMAIN,
    PROFILE_AUTO,
)
from custom_components.scpi_psu.scpi_psu_client.models import PsuIdentity


def _make_mock_client(
    ident: PsuIdentity | None = None,
    *,
    is_connected: bool = True,
    probe_error: Exception | None = None,
) -> MagicMock:
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.close = AsyncMock()
    mock_client.is_connected = is_connected
    if probe_error is not None:
        mock_client.probe_identity = AsyncMock(side_effect=probe_error)
    else:
        mock_client.probe_identity = AsyncMock(
            return_value=ident
            or PsuIdentity(
                manufacturer="OWON",
                model="SPE6103",
                serial_number="2138123",
                firmware="V1.2.3",
                raw_idn="OWON,SPE6103,2138123,V1.2.3",
                profile_key="owon",
                hw_max_voltage=60.0,
                hw_max_current=10.0,
            )
        )
    return mock_client


async def test_flow_user_step_and_confirm(hass: HomeAssistant) -> None:
    flow = ScpiPsuConfigFlow()
    flow.hass = hass
    flow.context = {}

    result = await flow.async_step_user()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    disconnected_client = _make_mock_client(is_connected=False)
    with patch(
        "custom_components.scpi_psu.config_flow.ScpiPsuClient",
        return_value=disconnected_client,
    ):
        err_result = await flow.async_step_user(
            {
                CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
                CONF_BAUDRATE: 115200,
            }
        )
        assert err_result["type"] is FlowResultType.FORM
        assert err_result["errors"] == {"base": "cannot_connect"}
    disconnected_client.close.assert_awaited_once()

    connected_client = _make_mock_client()
    with patch(
        "custom_components.scpi_psu.config_flow.ScpiPsuClient",
        return_value=connected_client,
    ):
        result2 = await flow.async_step_user(
            {
                CONF_SERIAL_PORT: "socket://127.0.0.1:5025",
                CONF_BAUDRATE: 115200,
            }
        )
        assert result2["type"] is FlowResultType.FORM
        assert result2["step_id"] == "confirm"
    connected_client.close.assert_awaited_once()

    result3 = await flow.async_step_confirm(
        {
            CONF_MAX_VOLTAGE_CLAMP: 24.0,
            CONF_MAX_CURRENT_CLAMP: 3.0,
            CONF_PROFILE: PROFILE_AUTO,
        }
    )
    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "OWON SPE6103"
    assert result3["data"][CONF_SERIAL_PORT] == "socket://127.0.0.1:5025"
    assert result3["data"][CONF_PROFILE] == PROFILE_AUTO
    assert result3["data"][CONF_MAX_VOLTAGE_CLAMP] == 24.0
    assert result3["data"][CONF_MAX_CURRENT_CLAMP] == 3.0


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test PSU",
        data={CONF_SERIAL_PORT: "socket://127.0.0.1:5025", CONF_BAUDRATE: 115200},
        entry_id="test_opts_entry",
    )
    flow = ScpiPsuConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, ScpiPsuOptionsFlow)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    timeout_client = _make_mock_client(probe_error=SerialException("Timed out"))
    with patch(
        "custom_components.scpi_psu.config_flow.ScpiPsuClient",
        return_value=timeout_client,
    ):
        err_res = await flow.async_step_init(
            {
                CONF_SERIAL_PORT: "socket://127.0.0.1:5026",
                CONF_BAUDRATE: 115200,
                CONF_SCAN_INTERVAL: 5,
            }
        )
        assert err_res["type"] is FlowResultType.FORM
        assert err_res["errors"] == {"base": "cannot_connect"}
    timeout_client.close.assert_awaited_once()

    ok_client = _make_mock_client()
    with patch(
        "custom_components.scpi_psu.config_flow.ScpiPsuClient",
        return_value=ok_client,
    ):
        ok_res = await flow.async_step_init(
            {
                CONF_SERIAL_PORT: "socket://127.0.0.1:5026",
                CONF_BAUDRATE: 115200,
                CONF_SCAN_INTERVAL: 5,
                CONF_PROFILE: PROFILE_AUTO,
                CONF_MAX_VOLTAGE_CLAMP: 30.0,
                CONF_MAX_CURRENT_CLAMP: 5.0,
            }
        )
        assert ok_res["type"] is FlowResultType.CREATE_ENTRY
        assert ok_res["data"][CONF_SERIAL_PORT] == "socket://127.0.0.1:5026"
        assert ok_res["data"][CONF_PROFILE] == PROFILE_AUTO
        assert ok_res["data"][CONF_SCAN_INTERVAL] == 5
        assert ok_res["data"][CONF_MAX_VOLTAGE_CLAMP] == 30.0
    ok_client.close.assert_awaited_once()
