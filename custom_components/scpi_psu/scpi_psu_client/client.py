"""Async SCPI client using serialx transport."""

from __future__ import annotations

import asyncio
import logging
import time
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
)

_LOGGER = logging.getLogger(__name__)


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
        self.planner: QueryPlanner = QueryPlanner(
            self.profile, self.identity.channels, self.identity.has_dmm
        )

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    @property
    def profile(self) -> ScpiProfile:
        if self.configured_profile and self.configured_profile in PROFILES:
            return PROFILES[self.configured_profile]
        return PROFILES.get(self.identity.profile_key, PROFILES[PROFILE_STANDARD])

    def default_fields_by_channel(self) -> dict[int, set[str]]:
        """Return the default telemetry fields to poll for each hardware channel."""
        ch1_fields = {
            FIELD_VOLTAGE,
            FIELD_CURRENT,
            FIELD_POWER,
            FIELD_OUTPUT_ENABLED,
            FIELD_STATUS_TEXT,
            FIELD_FAULT_ALARM,
            FIELD_OVP_TRIPPED,
            FIELD_OCP_TRIPPED,
            FIELD_OPP_TRIPPED,
        }
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

    async def _close_transport_unlocked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.5)
            except (OSError, SerialException, TimeoutError) as err:
                _LOGGER.debug("Error while closing transport to %s: %s", self.serial_url, err)
            self._writer = None
            self._reader = None
        self._connected = False

    async def _ensure_connected_unlocked(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            self._connected = True
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
            self._connected = True
            _LOGGER.info("Connected to SCPI PSU at %s", self.serial_url)
            if self.write_delay > 0:
                await asyncio.sleep(self.write_delay)
            return True
        except (OSError, SerialException, TimeoutError) as err:
            _LOGGER.debug("serialx connect to %s failed: %s", self.serial_url, err)
            self._connected = False
            return False

    async def query(self, cmd: str, expect_reply: bool = True) -> str | None:
        """Send an SCPI query and return response line."""
        async with self._lock:
            return await self._query_unlocked(cmd, expect_reply=expect_reply)

    async def _query_unlocked(self, cmd: str, expect_reply: bool = True) -> str | None:
        if not await self._ensure_connected_unlocked():
            return None

        payload = (cmd.strip() + "\n").encode("ascii", errors="ignore")
        start_t = time.monotonic()

        try:
            assert self._writer is not None
            assert self._reader is not None
            self._writer.write(payload)
            await asyncio.wait_for(self._writer.drain(), timeout=1.5)
            if not expect_reply:
                if self.write_delay > 0:
                    await asyncio.sleep(self.write_delay)
                elapsed_ms = (time.monotonic() - start_t) * 1000.0
                self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=True)
                return ""
            raw_line = await asyncio.wait_for(self._reader.readline(), timeout=1.8)
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            if not raw_line:
                self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
                await self._close_transport_unlocked()
                return None
            if self.command_delay > 0:
                await asyncio.sleep(self.command_delay)
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=True)
            return raw_line.decode("utf-8", errors="ignore").strip()

        except TimeoutError:
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
            _LOGGER.debug("SCPI timeout waiting for reply to '%s'", cmd)
            return None
        except (OSError, SerialException) as err:
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            self.planner.latency_tracker.record_latency(cmd, elapsed_ms, success=False)
            _LOGGER.warning("serialx transport error on '%s': %s", cmd, err)
            await self._close_transport_unlocked()
            return None

    def set_identity(self, identity: PsuIdentity) -> None:
        """Update client identity and reconfigure query planner capabilities."""
        self.identity = identity
        self.planner = QueryPlanner(self.profile, self.identity.channels, self.identity.has_dmm)

    async def probe_identity(self) -> PsuIdentity:
        async with self._lock:
            raw_idn = await self._query_unlocked("*IDN?")
            self.set_identity(parse_idn(raw_idn))
            return self.identity

    async def select_channel(self, channel: int) -> bool:
        cmd = self.profile.format_select_channel(channel)
        async with self._lock:
            await self._query_unlocked(cmd, expect_reply=False)
            return True

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

        results: dict[str, Any] = {}
        for desc in plan:
            raw = await self._query_unlocked(desc.command)
            if desc.parser:
                parsed = desc.parser(raw)
                results.update(parsed)

        if not results:
            return None

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

        outp = results.get(FIELD_OUTPUT_ENABLED)
        if outp is None and FIELD_OUTPUT_ENABLED in fields and self.identity.channels > 1:
            outp = await self._get_output_unlocked(channel=channel)

        status = results.get(FIELD_STATUS_TEXT)
        if status is None and FIELD_STATUS_TEXT in fields:
            status = "Active" if outp is not False else "Standby / Output Off"

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
        async with self._lock:
            return await self._poll_channel_unlocked(channel=channel, needed_fields=needed_fields)

    async def _get_output_unlocked(self, channel: int = 1) -> bool | None:
        if self.identity.channels > 1:
            cmd = self.profile.format_get_output(channel)
            raw = await self._query_unlocked(cmd)
            parsed = parse_output_state(raw)
            if parsed is not None:
                return parsed
            if channel > 1:
                await self._query_unlocked(
                    self.profile.format_select_channel(channel), expect_reply=False
                )
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
        for ch in range(1, total_ch + 1):
            ch_fields = field_map.get(ch)
            if not ch_fields:
                continue
            snap = await self.poll_telemetry(channel=ch, needed_fields=ch_fields)
            if snap is None:
                if ch == 1:
                    return None
                continue
            results[ch] = snap
        return results if results else None

    async def _set_and_verify_float(self, set_cmd: str, get_cmd: str, fallback: float) -> float:
        async with self._lock:
            await self._query_unlocked(set_cmd, expect_reply=False)
            readback = parse_strict_float(await self._query_unlocked(get_cmd))
            return readback if readback is not None else fallback

    async def get_output(self, channel: int = 1) -> bool | None:
        async with self._lock:
            return await self._get_output_unlocked(channel=channel)

    async def set_output(self, enabled: bool, channel: int = 1) -> bool:
        async with self._lock:
            if self.identity.channels > 1:
                cmd = self.profile.format_set_output(enabled, channel)
                await self._query_unlocked(cmd, expect_reply=False)
                # Verify readback
                get_cmd = self.profile.format_get_output(channel)
                parsed = parse_output_state(await self._query_unlocked(get_cmd))
                if parsed is not None:
                    return parsed
                await self._query_unlocked(
                    self.profile.format_select_channel(channel), expect_reply=False
                )
            else:
                cmd = self.profile.format_set_output(enabled)
                await self._query_unlocked(cmd, expect_reply=False)

            parsed = parse_output_state(await self._query_unlocked("OUTP?"))
            return parsed if parsed is not None else enabled

    async def set_all_outputs(self, enabled: bool) -> bool:
        """Atomically set all outputs on a multi-channel power supply."""
        async with self._lock:
            # Try atomic broadcast command OUTP:ALL
            cmd = self.profile.format_set_all_outputs(enabled)
            await self._query_unlocked(cmd, expect_reply=False)
            for ch in range(1, self.identity.channels + 1):
                # Ensure each channel state matches
                ch_cmd = self.profile.format_set_output(enabled, ch)
                await self._query_unlocked(ch_cmd, expect_reply=False)
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
            return err if err else None

    async def _query_bounded_float_unlocked(self, cmd: str, max_val: float) -> float | None:
        val = parse_strict_float(await self._query_unlocked(cmd))
        return round(val, 3) if val is not None and 0.0 <= val <= max_val else None

    async def _get_setpoints_unlocked(
        self, max_v_hw: float, max_i_hw: float
    ) -> dict[str, float | None]:
        prof = self.profile
        return {
            "voltage_setpoint": await self._query_bounded_float_unlocked(
                prof.cmd_get_voltage_setpoint, max_v_hw
            ),
            "current_setpoint": await self._query_bounded_float_unlocked(
                prof.cmd_get_current_setpoint, max_i_hw
            ),
        }

    async def get_setpoints(self) -> dict[str, float | None]:
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        async with self._lock:
            return await self._get_setpoints_unlocked(max_v_hw, max_i_hw)

    async def get_config_registers(self) -> dict[str, float | None]:
        prof = self.profile
        max_v_hw = self.identity.hw_max_voltage * 1.15
        max_i_hw = self.identity.hw_max_current * 1.15

        async with self._lock:
            return {
                "voltage_limit": await self._query_bounded_float_unlocked(
                    prof.cmd_get_voltage_limit, max_v_hw
                ),
                "current_limit": await self._query_bounded_float_unlocked(
                    prof.cmd_get_current_limit, max_i_hw
                ),
                **(await self._get_setpoints_unlocked(max_v_hw, max_i_hw)),
            }

    async def clear_faults(self) -> None:
        async with self._lock:
            await self._query_unlocked("*CLS", expect_reply=False)
