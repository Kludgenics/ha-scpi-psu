import pytest
from scpi_psu_client import (
    parse_idn,
    parse_strict_float,
    OwonScpiProfile,
    StandardScpiProfile,
    PROFILES,
    PsuIdentity,
)

def test_parse_idn_owon():
    idn = "OWON,SPE6103,2138123,V1.2.3"
    ident = parse_idn(idn)
    assert ident.manufacturer == "OWON"
    assert ident.model == "SPE6103"
    assert ident.serial_number == "2138123"
    assert ident.firmware == "V1.2.3"
    assert ident.profile_key == "owon"
    assert ident.hw_max_voltage == 60.0
    assert ident.hw_max_current == 10.0
    assert ident.default_title == "OWON SPE6103"

def test_parse_idn_standard_generic():
    idn = "Rigol Technologies,DP832,DP8B1234567,00.01.14"
    ident = parse_idn(idn)
    assert ident.manufacturer == "Rigol Technologies"
    assert ident.model == "DP832"
    assert ident.profile_key == "standard"
    assert ident.hw_max_voltage == 30.0
    assert ident.hw_max_current == 3.0

def test_parse_strict_float():
    assert parse_strict_float("  12.345 \n") == 12.345
    assert parse_strict_float(None) is None
    assert parse_strict_float("") is None
    assert parse_strict_float("invalid") is None

def test_owon_meas_all_info():
    profile = OwonScpiProfile()
    line = "12.000, 1.500, 18.000, 0, 0, 0, 1"
    snap = profile.parse_meas_all_info(line)
    assert snap is not None
    assert snap.voltage == 12.0
    assert snap.current == 1.5
    assert snap.power == 18.0
    assert snap.ovp_tripped is False
    assert snap.ocp_tripped is False
    assert snap.opp_tripped is False
    assert snap.status_code == 1
    assert snap.status_text == "Constant Voltage (CV)"
    assert snap.output_enabled is True
    assert snap.fault_alarm is False

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from scpi_psu_client import ScpiPsuClient

@pytest.mark.asyncio
async def test_scpi_client_mock_telemetry():
    client = ScpiPsuClient("socket://127.0.0.1:5025", profile_key="owon")
    client._writer = MagicMock()
    client._writer.is_closing.return_value = False
    client._connected = True

    with patch.object(client, "_query_unlocked", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = "30.000, 2.500, 75.000, 0, 0, 0, 1"
        snap = await client.poll_telemetry()
        assert snap is not None
        assert snap.voltage == 30.0
        assert snap.current == 2.5
        assert snap.power == 75.0
        assert snap.status_text == "Constant Voltage (CV)"

@pytest.mark.asyncio
async def test_scpi_client_mock_controls():
    client = ScpiPsuClient("socket://127.0.0.1:5025", profile_key="owon")
    client._writer = MagicMock()
    client._writer.is_closing.return_value = False
    client._connected = True

    with patch.object(client, "_query_unlocked", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = "ON"
        res = await client.set_output(True)
        assert res is True

        mock_query.return_value = "15.000"
        v = await client.set_voltage(15.0)
        assert v == 15.0
