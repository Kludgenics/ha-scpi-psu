"""TCP to PyVISA-sim bridge for testing serialx socket:// URIs against simulated SCPI hardware."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pyvisa


class PyVisaSimTcpBridge:
    """Async TCP Server bridge directing incoming SCPI commands to pyvisa-sim resource."""

    def __init__(
        self,
        resource_name: str,
        sim_yaml_path: str | None = None,
        custom_query_hook: Any = None,
    ) -> None:
        self.resource_name = resource_name
        self.sim_yaml_path = sim_yaml_path
        self.custom_query_hook = custom_query_hook
        self._server: asyncio.Server | None = None
        self.port: int = 0
        self._inst: Any = None
        self._rm: Any = None
        self._client_tasks: set[asyncio.Task] = set()

    async def start(self) -> str:
        """Start TCP server on an ephemeral loopback port and return socket:// URL."""
        sim_spec = f"{self.sim_yaml_path}@sim" if self.sim_yaml_path else "@sim"
        self._rm = pyvisa.ResourceManager(sim_spec)
        self._inst = self._rm.open_resource(self.resource_name)
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"

        self._server = await asyncio.start_server(self._handle_client_wrapper, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        return f"socket://127.0.0.1:{self.port}"

    def _handle_client_wrapper(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Process incoming raw lines and query the simulated pyvisa instrument."""
        try:
            while not reader.at_eof():
                try:
                    line_bytes = await asyncio.wait_for(reader.readline(), timeout=0.5)
                except TimeoutError:
                    if self._server is None or not self._server.is_serving():
                        break
                    continue
                if not line_bytes:
                    break
                line = line_bytes.decode("ascii", errors="ignore").strip()
                if not line:
                    continue

                if "?" in line:
                    try:
                        reply: str | None = None
                        if self.custom_query_hook:
                            reply = self.custom_query_hook(line)
                        if reply is None:
                            # Support IEEE 488.2 compound queries separated by semicolons
                            if ";" in line:
                                subcmds = [c.strip().lstrip(":") for c in line.split(";")]
                                subreplies = [self._inst.query(sc).strip() for sc in subcmds]
                                reply = ";".join(subreplies)
                            else:
                                reply = self._inst.query(line)
                        writer.write((reply.strip() + "\n").encode("ascii"))
                        await writer.drain()
                    except Exception:
                        writer.write(b"ERROR\n")
                        await writer.drain()
                else:
                    consumed = False
                    if self.custom_query_hook:
                        consumed = bool(self.custom_query_hook(line))
                    if not consumed:
                        with contextlib.suppress(Exception):
                            self._inst.write(line)
        except asyncio.CancelledError, ConnectionResetError, BrokenPipeError, OSError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
            self._client_tasks.clear()

        if self._inst is not None:
            with contextlib.suppress(Exception):
                self._inst.close()
            self._inst = None
        if self._rm is not None:
            with contextlib.suppress(Exception):
                self._rm.close()
            self._rm = None
