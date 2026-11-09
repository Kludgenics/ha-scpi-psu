"""The simulator must not hide broken test hooks."""

import asyncio

import pytest

from .matrix import SIM_YAML
from .sim_bridge import PyVisaSimTcpBridge


@pytest.mark.enable_socket
async def test_unexpected_simulator_error_is_reported_at_teardown() -> None:
    def broken_hook(command: str) -> None:
        raise ValueError("broken simulator fixture")

    bridge = PyVisaSimTcpBridge("ASRL1::INSTR", SIM_YAML, custom_query_hook=broken_hook)
    await bridge.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    try:
        writer.write(b"*IDN?\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        with pytest.raises(
            ExceptionGroup, match="Unexpected simulated instrument errors"
        ) as raised:
            await bridge.stop()
        assert len(raised.value.exceptions) == 1
        assert isinstance(raised.value.exceptions[0], ValueError)
