"""Async SCPI client using serialx transport."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .dialects import PROFILES, ScpiProfile, parse_output_state
from .models import (
    DEFAULT_BAUDRATE,
    PROFILE_STANDARD,
    PsuIdentity,
    TelemetrySnapshot,
    parse_idn,
    parse_strict_float,
)
from .query_planner import (
    FIELD_CURRENT,
    FIELD_DMM,
    FIELD_FAULT_ALARM,
    FIELD_OCP_TRIPPED,
    FIELD_OPP_TRIPPED,
    FIELD_OUTPUT_ENABLED,
    FIELD_OVP_TRIPPED,
    FIELD_POWER,
    FIELD_STATUS_TEXT,
    FIELD_VOLTAGE,
    QueryPlanner,
    compound_command,
    split_compound_response,
)

_LOGGER = logging.getLogger(__name__)


class _MalformedCompoundReply(Exception):
    """A compound reply cannot be assigned to its component queries."""


class ScpiPsuClient:
    """Serialized async client for SCPI programmable power supplies."""

    def __init__(
        self,
        serial_url: str,
        baudrate: int = DEFAULT_BAUDRATE,
        initial_idn: str | None = None,
        profile_key: str | None = None,
        command_delay: float = 0.10,
        write_delay: float = 0.15,
    ) -> None:
        self.serial_url = serial_url
        self.baudrate = baudrate
        self.identity: PsuIdentity = parse_idn(initial_idn)
        self.configured_profile: str | None = profile_key
        self.command_delay = command_delay
        self.write_delay = write_delay
        self._compound_queries_enabled = True
        self._needs_resynchronization = False
        self._pending_identity_replies = 0
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    @property
    def profile(self) -> ScpiProfile:
        if self.configured_profile and self.configured_profile in PROFILES:
            return PROFILES[self.configured_profile]
        return PROFILES.get(self.identity.profile_key, PROFILES[PROFILE_STANDARD])

    @property
    def planner(self) -> QueryPlanner:
        return QueryPlanner(
            self.profile, self.identity.has_dmm, compound_queries=self._compound_queries_enabled
        )

    def default_fields_by_channel(self) -> dict[int, set[str]]:
        """Return the default telemetry fields to poll for each hardware channel."""
        prof = self.profile
        ch1_fields = {
            FIELD_VOLTAGE,
            FIELD_CURRENT,
            FIELD_POWER,
            FIELD_OUTPUT_ENABLED,
            FIELD_STATUS_TEXT,
        }
        if prof.reports_status_in_telemetry:
            ch1_fields.add(FIELD_FAULT_ALARM)
        if prof.supports_ovp:
            ch1_fields.add(FIELD_OVP_TRIPPED)
        if prof.supports_ocp:
            ch1_fields.add(FIELD_OCP_TRIPPED)
        if prof.supports_opp:
            ch1_fields.add(FIELD_OPP_TRIPPED)
        if self.identity.has_dmm:
            ch1_fields.add(FIELD_DMM)
        result: dict[int, set[str]] = {1: ch1_fields}
        for ch in range(2, self.identity.channels + 1):
            result[ch] = {FIELD_OUTPUT_ENABLED}
        return result

    async def connect(self) -> None:
        async with self._lock:
            await self._ensure_connected_unlocked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_transport_unlocked()
            self._needs_resynchronization = False
            self._pending_identity_replies = 0

    async def _close_transport_unlocked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.5)
            except (OSError, SerialException, TimeoutError) as err:
                _LOGGER.debug("Error while closing transport to %s: %s", self.serial_url, err)
            self._writer = None
            self._reader = None

    def _flush_rx_unlocked(self) -> None:
        """Purge unread characters from transport and StreamReader internal buffer."""
        # 1. Reset underlying hardware/OS serial port read buffer if supported
        if self._writer is not None:
            transport = getattr(self._writer, "transport", None)
            if transport is not None:
                # serialx BaseSerialTransport / PosixSerialTransport
                serial_obj = getattr(transport, "serial", None) or getattr(
                    transport, "_serial", None
                )
                if serial_obj is not None:
                    reset_fn = getattr(serial_obj, "reset_read_buffer", None) or getattr(
                        serial_obj, "reset_input_buffer", None
                    )
                    if callable(reset_fn):
                        with suppress(Exception):
                            reset_fn()

        # 2. Clear StreamReader's in-memory buffer
        if self._reader is not None:
            buf = getattr(self._reader, "_buffer", None)
            if buf is not None and hasattr(buf, "clear"):
                buf.clear()

    async def _ensure_connected_unlocked(self) -> bool:
        if self.is_connected:
            return True

        await self._close_transport_unlocked()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                open_serial_connection(
                    url=self.serial_url,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity=Parity.NONE,
                    stopbits=StopBits.ONE,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                ),
                timeout=3.5,
            )
            _LOGGER.info("Connected to SCPI PSU at %s", self.serial_url)
            if self.write_delay > 0:
                await asyncio.sleep(self.write_delay)
            self._flush_rx_unlocked()
            if self._needs_resynchronization:
                if not await self._resynchronize_unlocked():
                    await self._close_transport_unlocked()
                    return False
                self._needs_resynchronization = False
            if self.identity.channels > 1:
                selected = await self._exchange_unlocked(
                    self.profile.format_select_channel(1), expect_reply=False
                )
                if selected is None:
                    return False
            return True
        except asyncio.CancelledError:
            await self._close_transport_unlocked()
            raise
        except (OSError, SerialException, TimeoutError) as err:
            _LOGGER.debug("serialx connect to %s failed: %s", self.serial_url, err)
            await self._close_transport_unlocked()
            return False

    @staticmethod
    def _is_structured_identity(line: str) -> bool:
        parts = line.split(",")
        return len(parts) == 4 and any(c.isalpha() for c in parts[0])

    def _is_identity_reply(self, line: str) -> bool:
        if self.identity.is_valid:
            return line == self.identity.raw_idn.strip()
        return self._is_structured_identity(line)

    async def _invalidate_session_unlocked(self) -> None:
        self._needs_resynchronization = True
        await self._close_transport_unlocked()

    async def _resynchronize_unlocked(self) -> bool:
        """Drain through every outstanding identity reply before reusing the serial link.

        SCPI replies must remain ordered and this client must be the only controller.
        A lost identity reply leaves recovery incomplete rather than admitting an
        older marker as the boundary for a newer exchange.
        """
        assert self._writer is not None
        assert self._reader is not None
        try:
            async with asyncio.timeout(3.5):
                self._pending_identity_replies += 1
                self._writer.write(b"*IDN?\n")
                await self._writer.drain()
                for _ in range(32):
                    raw = await self._reader.readline()
                    if not raw:
                        return False
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    if self._is_identity_reply(line):
                        self._pending_identity_replies -= 1
                        if self._pending_identity_replies == 0:
                            return True
        except (OSError, SerialException, TimeoutError):
            return False
        return False

    async def query(self, cmd: str, expect_reply: bool = True) -> str | None:
        """Send an SCPI query and return response line."""
        async with self._lock:
            return await self._query_unlocked(cmd, expect_reply=expect_reply)

    async def _query_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        if not await self._ensure_connected_unlocked():
            return None

        return await self._exchange_unlocked(cmd, expect_reply)

    async def _exchange_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        payload = (cmd.strip() + "\n").encode("ascii", errors="ignore")

        identity_query = expect_reply and cmd.strip().upper() == "*IDN?"
        try:
            assert self._writer is not None
            assert self._reader is not None
            if identity_query:
                self._pending_identity_replies += 1
            self._writer.write(payload)
            await asyncio.wait_for(self._writer.drain(), timeout=1.5)
            if not expect_reply:
                if self.write_delay > 0:
                    await asyncio.sleep(self.write_delay)
                return ""

            raw_line = b""
            for _ in range(16):
                raw = await asyncio.wait_for(self._reader.readline(), timeout=1.8)
                if not raw:
                    await self._invalidate_session_unlocked()
                    return None
                if raw.strip():
                    raw_line = raw
                    break
            else:
                await self._invalidate_session_unlocked()
                return None

            line = raw_line.decode("utf-8", errors="ignore").strip()
            if identity_query:
                if not self._is_structured_identity(line):
                    await self._invalidate_session_unlocked()
                    return None
                self._pending_identity_replies -= 1
            if self.command_delay > 0:
                await asyncio.sleep(self.command_delay)
            return line

        except TimeoutError:
            _LOGGER.debug("SCPI timeout waiting for reply to '%s'", cmd)
            await self._invalidate_session_unlocked()
            return None
        except asyncio.CancelledError:
            await self._invalidate_session_unlocked()
            raise
        except (OSError, SerialException) as err:
            _LOGGER.warning("serialx transport error on '%s': %s", cmd, err)
            await self._invalidate_session_unlocked()
            return None

    async def _query_compound_unlocked(self, cmd: str, count: int) -> str | None:
        raw = await self._query_unlocked(cmd)
        if not self.is_connected:
            self._compound_queries_enabled = False
            return None
        if split_compound_response(raw, count) is None:
            await self._disable_compound_queries_unlocked()
            raise _MalformedCompoundReply
        return raw

    @asynccontextmanager
    async def _restore_primary_channel_unlocked(self, restore: bool = True) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if restore and self.is_connected and self.identity.channels > 1:
                await self._query_unlocked(
                    self.profile.format_select_channel(1), expect_reply=False
                )

    async def probe_identity(self) -> PsuIdentity:
        async with self._lock:
            raw_idn = await self._query_unlocked("*IDN?")
            candidate = parse_idn(raw_idn)
            if candidate.is_valid or not self.identity.is_valid:
                self.identity = candidate
            if self.is_connected and self.identity.channels > 1:
                await self._query_unlocked(
                    self.profile.format_select_channel(1), expect_reply=False
                )
            return self.identity

    async def select_channel(self, channel: int) -> bool:
        cmd = self.profile.format_select_channel(channel)
        async with self._lock:
            return await self._query_unlocked(cmd, expect_reply=False) is not None

    async def _poll_channel_unlocked(
        self,
        channel: int = 1,
        needed_fields: set[str] | None = None,
    ) -> TelemetrySnapshot | None:
        prof = self.profile
        fields = (
            needed_fields
            if needed_fields is not None
            else self.default_fields_by_channel().get(channel, {FIELD_OUTPUT_ENABLED})
        )
        if not fields:
            return None

        plan = self.planner.plan_queries(fields, channel=channel)
        if not plan:
            return None

        if self.identity.channels > 1:
            await self._query_unlocked(prof.format_select_channel(channel), expect_reply=False)
            if not self.is_connected:
                return None

        results: dict[str, Any] = {}
        for desc in plan:
            try:
                raw = (
                    await self._query_compound_unlocked(desc.command, desc.component_count)
                    if desc.component_count > 1
                    else await self._query_unlocked(desc.command)
                )
            except _MalformedCompoundReply:
                return await self._poll_channel_unlocked(channel, needed_fields)
            if not self.is_connected:
                return None
            if desc.parser:
                parsed = desc.parser(raw)
                results.update(parsed)

        if not results:
            return None

        outp = results.get(FIELD_OUTPUT_ENABLED)

        raw_v = results.get(FIELD_VOLTAGE)
        raw_i = results.get(FIELD_CURRENT)
        raw_p = results.get(FIELD_POWER)

        # Require requested primary measurements to succeed
        if FIELD_VOLTAGE in fields and raw_v is None:
            return None
        if FIELD_CURRENT in fields and raw_i is None:
            return None

        val_v = float(raw_v) if raw_v is not None else None
        val_i = float(raw_i) if raw_i is not None else None
        if raw_p is not None:
            val_p: float | None = float(raw_p)
        elif val_v is not None and val_i is not None:
            val_p = round(val_v * val_i, 3)
        else:
            val_p = None

        status = results.get(FIELD_STATUS_TEXT)
        if status is None and FIELD_STATUS_TEXT in fields and outp is not None:
            status = "Active" if outp else "Standby / Output Off"

        return TelemetrySnapshot(
            voltage=val_v,
            current=val_i,
            power=val_p,
            output_enabled=outp,
            status_text=str(status) if status is not None else None,
            fault_alarm=results.get(FIELD_FAULT_ALARM),
            ovp_tripped=results.get(FIELD_OVP_TRIPPED),
            ocp_tripped=results.get(FIELD_OCP_TRIPPED),
            opp_tripped=results.get(FIELD_OPP_TRIPPED),
            dmm_reading=results.get(FIELD_DMM),
            channel=channel,
        )

    async def poll_telemetry(
        self,
        channel: int = 1,
        needed_fields: set[str] | None = None,
    ) -> TelemetrySnapshot | None:
        async with self._lock, self._restore_primary_channel_unlocked(restore=channel != 1):
            return await self._poll_channel_unlocked(channel=channel, needed_fields=needed_fields)

    async def _get_output_unlocked(self, channel: int = 1) -> bool | None:
        if self.identity.channels > 1:
            if (
                channel == 1
                and await self._query_unlocked(
                    self.profile.format_select_channel(1), expect_reply=False
                )
                is None
            ):
                return None
            cmd = self.profile.format_get_output(channel)
            parsed = parse_output_state(await self._query_unlocked(cmd))
            if parsed is not None:
                return parsed
            if not self.is_connected:
                return None
            if channel > 1:
                await self._query_unlocked(
                    self.profile.format_select_channel(channel), expect_reply=False
                )
                if not self.is_connected:
                    return None
                async with self._restore_primary_channel_unlocked():
                    return parse_output_state(await self._query_unlocked("OUTP?"))
        return parse_output_state(await self._query_unlocked("OUTP?"))

    async def poll_all_channels(
        self,
        needed_fields_by_channel: dict[int, set[str]] | None = None,
    ) -> dict[int, TelemetrySnapshot] | None:
        """Poll telemetry across all configured channels."""
        total_ch = max(1, self.identity.channels)
        field_map = (
            needed_fields_by_channel
            if needed_fields_by_channel is not None
            else self.default_fields_by_channel()
        )
        results: dict[int, TelemetrySnapshot] = {}
        async with self._lock, self._restore_primary_channel_unlocked():
            for ch in range(1, total_ch + 1):
                ch_fields = field_map.get(ch)
                if not ch_fields:
                    continue
                snap = await self._poll_channel_unlocked(channel=ch, needed_fields=ch_fields)
                if snap is None:
                    if ch == 1 or not self.is_connected:
                        return None
                    continue
                results[ch] = snap
        return results if results else None

    async def _set_and_verify_float(self, set_cmd: str, get_cmd: str, fallback: float) -> float:
        async with self._lock:
            await self._write_unlocked(set_cmd)
            readback = parse_strict_float(await self._query_unlocked(get_cmd))
            return readback if readback is not None else fallback

    async def _write_unlocked(self, cmd: str) -> None:
        if await self._query_unlocked(cmd, expect_reply=False) is None:
            raise ConnectionError(f"Failed to send SCPI command {cmd!r} to {self.serial_url}")

    async def get_output(self, channel: int = 1) -> bool | None:
        async with self._lock:
            return await self._get_output_unlocked(channel=channel)

    async def set_output(self, enabled: bool, channel: int = 1) -> bool:
        async with self._lock:
            if self.identity.channels > 1 and channel == 1:
                await self._write_unlocked(self.profile.format_select_channel(1))
            cmd = self.profile.format_set_output(enabled, channel)
            await self._write_unlocked(cmd)
            parsed = await self._get_output_unlocked(channel=channel)
            return parsed if parsed is not None else enabled

    async def set_all_outputs(self, enabled: bool) -> bool:
        """Set all outputs on a multi-channel power supply."""
        async with self._lock:
            if self.identity.channels > 1:
                await self._write_unlocked(self.profile.format_select_channel(1))
            cmd = self.profile.format_set_all_outputs(enabled)
            await self._write_unlocked(cmd)
            for ch in range(1, self.identity.channels + 1):
                ch_cmd = self.profile.format_set_output(enabled, ch)
                await self._write_unlocked(ch_cmd)
            return enabled

    async def set_voltage(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_voltage(value),
            self.profile.cmd_get_voltage_setpoint,
            value,
        )

    async def set_current(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_current(value),
            self.profile.cmd_get_current_setpoint,
            value,
        )

    async def set_voltage_limit(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_voltage_limit(value),
            self.profile.cmd_get_voltage_limit,
            value,
        )

    async def set_current_limit(self, value: float) -> float:
        return await self._set_and_verify_float(
            self.profile.format_set_current_limit(value),
            self.profile.cmd_get_current_limit,
            value,
        )

    async def get_system_error(self) -> str | None:
        async with self._lock:
            err = await self._query_unlocked("SYST:ERR?")
            if err is None:
                return None
            if re.fullmatch(r'(?:[+-]?\d+\s*,\s*(?:"[^"\r\n]*"|[^,\r\n]+)|0[xX][0-9a-fA-F]+)', err):
                return err
            _LOGGER.warning(
                "Unexpected reply to SYST:ERR?; discarding unsynchronized session: %r", err
            )
            await self._invalidate_session_unlocked()
            return None

    async def _query_bounded_float_unlocked(self, cmd: str, max_val: float) -> float | None:
        val = parse_strict_float(await self._query_unlocked(cmd))
        return round(val, 3) if val is not None and 0.0 <= val <= max_val else None

    async def _query_float_group_unlocked(
        self, spec: list[tuple[str, str, float]]
    ) -> dict[str, float | None]:
        if (
            self._compound_queries_enabled
            and self.profile.supports_compound_queries
            and len(spec) > 1
        ):
            cmd = compound_command([c for _, c, _ in spec])
            try:
                raw = await self._query_compound_unlocked(cmd, len(spec))
            except _MalformedCompoundReply:
                if self.identity.channels > 1:
                    return dict.fromkeys(key for key, _, _ in spec)
            else:
                if raw is None:
                    return dict.fromkeys(key for key, _, _ in spec)
                parts = split_compound_response(raw, len(spec))
                assert parts is not None
                return {
                    key: (
                        round(v, 3)
                        if (v := parse_strict_float(part)) is not None and 0.0 <= v <= max_val
                        else None
                    )
                    for (key, _, max_val), part in zip(spec, parts, strict=True)
                }

        results: dict[str, float | None] = dict.fromkeys(key for key, _, _ in spec)
        for key, command, max_val in spec:
            results[key] = await self._query_bounded_float_unlocked(command, max_val)
            if not self.is_connected:
                break
        return results

    async def _disable_compound_queries_unlocked(self) -> None:
        """Discard a possibly misframed session and use scalar queries from now on."""
        self._compound_queries_enabled = False
        await self._invalidate_session_unlocked()

    def _setpoint_spec(self) -> list[tuple[str, str, float]]:
        prof = self.profile
        return [
            (
                "voltage_setpoint",
                prof.cmd_get_voltage_setpoint,
                self.identity.hw_max_voltage * 1.15,
            ),
            (
                "current_setpoint",
                prof.cmd_get_current_setpoint,
                self.identity.hw_max_current * 1.15,
            ),
        ]

    async def get_setpoints(self) -> dict[str, float | None]:
        async with self._lock:
            return await self._query_float_group_unlocked(self._setpoint_spec())

    async def get_config_registers(self) -> dict[str, float | None]:
        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15
        spec = [
            ("voltage_limit", prof.cmd_get_voltage_limit, max_v_hw),
            ("current_limit", prof.cmd_get_current_limit, max_i_hw),
            *self._setpoint_spec(),
        ]
        async with self._lock:
            return await self._query_float_group_unlocked(spec)

    async def clear_faults(self) -> None:
        async with self._lock:
            await self._write_unlocked("*CLS")
