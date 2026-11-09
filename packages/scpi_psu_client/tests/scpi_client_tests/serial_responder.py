"""Command-driven serial transport for client and integration tests."""

import asyncio
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


@contextmanager
def mock_serial_responder(responder: Any, *, client_module: str = "scpi_psu_client.client") -> Any:
    """Patch open_serial_connection with a command-driven responder."""
    pending_line = b""

    def _on_write(data: bytes) -> None:
        nonlocal pending_line
        cmd = data.decode("ascii", errors="ignore").strip()
        ans = responder(cmd)
        pending_line = f"{ans}\r\n".encode("ascii") if ans is not None else b""

    async def _on_readline() -> bytes:
        return pending_line

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readline.side_effect = _on_readline
    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_writer.write.side_effect = _on_write
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()

    with patch(
        f"{client_module}.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(mock_reader, mock_writer),
    ):
        yield mock_writer
