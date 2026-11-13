"""Exhaustive simulator command coverage and TCP transport fuzzing tests."""

from __future__ import annotations

import asyncio
import random

import pytest
from scpi_psu_client import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_OUTPUT_ENABLED,
    FIELD_OVP_TRIPPED,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    ScpiPsuClient,
)

from .matrix import SIM_YAML
from .sim_bridge import PyVisaSimTcpBridge


async def _write_chunked(
    writer: asyncio.StreamWriter,
    payload: bytes,
    chunk_sizes: list[int],
    delay: float = 0.0,
) -> None:
    idx = 0
    step = 0
    while idx < len(payload):
        size = chunk_sizes[step % len(chunk_sizes)]
        writer.write(payload[idx : idx + size])
        await writer.drain()
        idx += size
        step += 1
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)


@pytest.mark.enable_socket
async def test_sim_exhaustive_owon_command_coverage() -> None:
    """Exercise every OWON SCPI command against pyvisa-sim with zero simulator errors."""
    bridge = PyVisaSimTcpBridge("ASRL6::INSTR", SIM_YAML)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0)
    try:
        ident = await client.probe_identity()
        assert ident.profile_key == "owon"
        assert ident.has_dmm is True

        full = await client.poll_telemetry()
        assert full is not None
        assert full.voltage == 12.0
        assert full.dmm_reading == 3.298

        dmm_only = await client.poll_telemetry(needed_fields={FIELD_DMM})
        assert dmm_only is not None
        assert dmm_only.dmm_reading == 3.298

        v_only = await client.poll_telemetry(needed_fields={FIELD_VOLTAGE, FIELD_STATUS_TEXT})
        assert v_only is not None
        assert v_only.voltage == 12.0

        regs = await client.get_config_registers()
        assert regs["voltage_setpoint"] == 12.0
        assert regs["voltage_limit"] == 60.0
        assert await client.get_system_error() == "0,No error"
        assert await client.get_output() is True

        assert await client.set_voltage(15.0) == 15.0
        assert await client.set_current(2.5) == 2.5
        assert await client.set_voltage_limit(45.0) == 45.0
        assert await client.set_current_limit(5.0) == 5.0
        assert await client.set_output(False) is False
        assert await client.set_output(True) is True
        await client.clear_faults()
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []
    observed = set(bridge.observed_commands)
    expected = {
        "*IDN?",
        "*CLS",
        "SYST:ERR?",
        "MEAS:ALL:INFO?",
        ":READ?",
        "VOLT?",
        "CURR?",
        "VOLT:LIM?",
        "CURR:LIM?",
        "OUTP?",
        "VOLT 15.000",
        "CURR 2.500",
        "VOLT:LIM 45.000",
        "CURR:LIM 5.000",
        "OUTP OFF",
        "OUTP ON",
    }
    assert expected.issubset(observed)


@pytest.mark.enable_socket
async def test_sim_exhaustive_standard_multichannel_command_coverage() -> None:
    """Exercise every Standard SCPI multi-channel command against pyvisa-sim."""
    bridge = PyVisaSimTcpBridge("ASRL3::INSTR", SIM_YAML)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0)
    try:
        ident = await client.probe_identity()
        assert ident.profile_key == "standard"
        assert ident.channels == 3

        all_snaps = await client.poll_all_channels()
        assert set(all_snaps) == {1, 2, 3}
        assert all_snaps[1] is not None and all_snaps[1].voltage == 24.0

        partial_snaps = await client.poll_all_channels(
            needed_fields_by_channel={
                1: {FIELD_VOLTAGE, FIELD_OVP_TRIPPED},
                2: {FIELD_CURRENT, FIELD_OUTPUT_ENABLED},
                3: {FIELD_OUTPUT_ENABLED},
            }
        )
        assert partial_snaps[1] is not None and partial_snaps[1].ovp_tripped is False
        assert partial_snaps[2] is not None and partial_snaps[2].current == 1.5
        assert partial_snaps[3] is not None and partial_snaps[3].output_enabled is True

        regs = await client.get_config_registers()
        assert regs["voltage_limit"] == 30.0
        assert regs["current_limit"] == 3.0
        assert await client.get_system_error() == "0,No error"

        assert await client.set_voltage(12.0) == 12.0
        assert await client.set_current(1.5) == 1.5
        assert await client.set_voltage_limit(25.0) == 25.0
        assert await client.set_current_limit(2.5) == 2.5
        assert await client.set_output(False, channel=2) is False
        assert await client.get_output(channel=2) is False
        assert await client.set_output(True, channel=2) is True
        await client.set_all_outputs(False)
        await client.set_all_outputs(True)
        await client.clear_faults()
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []
    observed = set(bridge.observed_commands)
    expected = {
        "*IDN?",
        "*CLS",
        "SYST:ERR?",
        "VOLT?",
        "CURR?",
        "VOLT:PROT?",
        "CURR:PROT?",
        "INST:NSEL 1",
        "INST:NSEL 2",
        "INST:NSEL 3",
        "OUTP? CH2",
        "OUTP CH2,OFF",
        "OUTP CH2,ON",
        "OUTP:ALL OFF",
        "OUTP:ALL ON",
        "VOLT 12.000",
        "CURR 1.500",
        "VOLT:PROT 25.000",
        "CURR:PROT 2.500",
    }
    assert expected.issubset(observed)
    assert any(
        "OUTP?" in cmd and "VOLT:PROT:TRIP?" in cmd and "CURR:PROT:TRIP?" in cmd for cmd in observed
    )


@pytest.mark.enable_socket
async def test_sim_tcp_fragmented_reads_and_crlf() -> None:
    """Verify serialx StreamReader handles byte-chunked TCP frames and CRLF endings."""
    bridge: PyVisaSimTcpBridge

    async def fragment_hook(line: str, writer: asyncio.StreamWriter) -> bool:
        if "?" not in line:
            return False
        reply = bridge.query_instrument(line)
        await _write_chunked(writer, (reply + "\r\n").encode("ascii"), [1, 2, 3])
        return True

    bridge = PyVisaSimTcpBridge("ASRL3::INSTR", SIM_YAML, async_transport_hook=fragment_hook)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0)
    try:
        ident = await client.probe_identity()
        assert ident.model == "DP832"

        snaps = await client.poll_all_channels()
        assert snaps[1] is not None and snaps[1].voltage == 24.0
        assert snaps[2] is not None and snaps[2].output_enabled is True
        assert snaps[3] is not None and snaps[3].output_enabled is True

        assert await client.set_voltage(14.5) == 14.5
        regs = await client.get_config_registers()
        assert regs["voltage_setpoint"] == 14.5
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []


@pytest.mark.enable_socket
async def test_sim_tcp_hang_timeout_and_reconnect_recovery() -> None:
    """A stalled TCP reply times out cleanly and recovers via *IDN? on reconnect."""
    bridge: PyVisaSimTcpBridge
    hang_once = True

    async def hang_hook(line: str, writer: asyncio.StreamWriter) -> bool:
        nonlocal hang_once
        if hang_once and line == "MEAS:ALL:INFO?":
            hang_once = False
            await asyncio.sleep(0.25)
            return True
        return False

    bridge = PyVisaSimTcpBridge("ASRL1::INSTR", SIM_YAML, async_transport_hook=hang_hook)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0, read_timeout=0.06)
    try:
        await client.probe_identity()
        assert client.is_connected

        assert await client.poll_telemetry() is None
        assert not client.is_connected

        recovered = await client.poll_telemetry()
        assert recovered is not None
        assert recovered.voltage == 12.0
        assert client.is_connected
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []


@pytest.mark.enable_socket
async def test_sim_tcp_mid_frame_drop_and_stale_banner_recovery() -> None:
    """Mid-frame TCP EOF and stale pre-reply noise recover cleanly on reconnect."""
    bridge: PyVisaSimTcpBridge
    drop_once = True
    inject_stale = True

    async def drop_and_stale_hook(line: str, writer: asyncio.StreamWriter) -> bool:
        nonlocal drop_once, inject_stale
        if drop_once and line == "MEAS:ALL:INFO?":
            drop_once = False
            writer.write(b"12.0, 2.")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return True
        if not drop_once and inject_stale and line == "*IDN?":
            inject_stale = False
            writer.write(b"UNSOLICITED_STALE_FRAME\n\r\n")
            await writer.drain()
            reply = bridge.query_instrument(line)
            writer.write((reply + "\n").encode("ascii"))
            await writer.drain()
            return True
        return False

    bridge = PyVisaSimTcpBridge("ASRL1::INSTR", SIM_YAML, async_transport_hook=drop_and_stale_hook)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0, read_timeout=0.2)
    try:
        await client.probe_identity()
        assert await client.poll_telemetry() is None
        assert not client.is_connected

        recovered = await client.poll_telemetry()
        assert recovered is not None
        assert recovered.voltage == 12.0
        assert recovered.current == 2.0
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []


@pytest.mark.enable_socket
async def test_sim_tcp_fuzz_transport_chaos() -> None:
    """Seeded fuzz test exercising chunked reads, CRLF, stalls, drops, and bad frames."""
    rng = random.Random(2026)
    bridge: PyVisaSimTcpBridge
    chaos_enabled = True

    async def chaos_hook(line: str, writer: asyncio.StreamWriter) -> bool:
        if not chaos_enabled or "?" not in line:
            return False

        roll = rng.random()
        if line != "*IDN?":
            if roll < 0.12:
                await asyncio.sleep(0.12)
                return True
            if roll < 0.24:
                writer.write(b"12.0,")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return True
            if roll < 0.34:
                writer.write(b"GARBAGE,FRAME\n")
                await writer.drain()
                return True

        reply = bridge.query_instrument(line)
        eol = "\r\n" if rng.random() < 0.5 else "\n"
        chunk_sizes = [rng.randint(1, 6) for _ in range(4)]
        await _write_chunked(writer, (reply + eol).encode("ascii"), chunk_sizes, delay=0.001)
        return True

    bridge = PyVisaSimTcpBridge("ASRL1::INSTR", SIM_YAML, async_transport_hook=chaos_hook)
    socket_url = await bridge.start()
    client = ScpiPsuClient(socket_url, command_delay=0.0, write_delay=0.0, read_timeout=0.05)
    try:
        await client.probe_identity()

        for step in range(25):
            op = step % 4
            try:
                if op == 0:
                    snap = await client.poll_telemetry()
                    if snap is not None:
                        assert 0.0 <= (snap.voltage or 0.0) <= 60.0
                        assert 0.0 <= (snap.current or 0.0) <= 10.0
                elif op == 1:
                    await client.set_voltage(round(5.0 + (step % 15), 2))
                elif op == 2:
                    await client.set_current(round(1.0 + (step % 5), 2))
                else:
                    await client.set_output(bool(step % 2))
            except (ConnectionError, OSError):
                pass

        chaos_enabled = False
        final_snap = await client.poll_telemetry()
        assert final_snap is not None
        assert final_snap.voltage is not None
        assert final_snap.current is not None
        assert client.is_connected
    finally:
        await client.close()
        await bridge.stop()

    assert bridge.error_commands == []
