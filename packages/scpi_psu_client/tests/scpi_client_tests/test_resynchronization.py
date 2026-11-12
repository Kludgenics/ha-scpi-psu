"""Recovery across delayed serial replies and interrupted identity barriers."""

import asyncio
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scpi_psu_client import ScpiPsuClient
from serialx import SerialException

IDN = "OWON,SPE6103,123,1"


def connection(replies: Sequence[bytes | BaseException]) -> tuple[AsyncMock, MagicMock]:
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.side_effect = replies
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


@pytest.mark.parametrize("initial_query", ["VOLT?", "*IDN?"])
@pytest.mark.parametrize("interruption", [TimeoutError, asyncio.CancelledError])
async def test_interrupted_recovery_accounts_for_all_identity_replies(
    initial_query: str, interruption: type[BaseException]
) -> None:
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    first = connection([TimeoutError()])
    interrupted = connection([interruption()])
    identities = 3 if initial_query == "*IDN?" else 2
    recovered = connection(
        [b"54.190,1.635,88.600,OFF,OFF,OFF,1\n", b"5.455\n"]
        + [(IDN + "\n").encode()] * identities
        + [b"4.000\n", b"2.000\n"]
    )
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[first, interrupted, recovered],
    ):
        assert await client.query(initial_query) is None
        if interruption is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await client.query("VOLT?")
        else:
            assert await client.query("VOLT?") is None
        assert not client.is_connected
        assert await client.query("VOLT?") == "4.000"
        assert await client.query("CURR?") == "2.000"
        assert [call.args[0] for call in recovered[1].write.call_args_list] == [
            b"*IDN?\n",
            b"VOLT?\n",
            b"CURR?\n",
        ]
    await client.close()


async def test_recovery_write_error_closes_uninitialized_transport() -> None:
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    first = connection([TimeoutError()])
    failed = connection([])
    failed[1].write.side_effect = SerialException("bridge unavailable")
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[first, failed],
    ):
        assert await client.query("VOLT?") is None
        assert await client.query("CURR?") is None
        assert not client.is_connected
        failed[1].close.assert_called_once()
        failed[1].write.assert_called_once_with(b"*IDN?\n")


async def test_missing_old_identity_reply_does_not_admit_new_query() -> None:
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    first = connection([TimeoutError()])
    recovery = connection([(IDN + "\n").encode(), b""])
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[first, recovery],
    ):
        assert await client.query("*IDN?") is None
        assert await client.query("VOLT?") is None
        assert not client.is_connected
        recovery[1].write.assert_called_once_with(b"*IDN?\n")


async def test_normal_reprobe_can_discover_changed_identity() -> None:
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    stream = connection([b"Rigol,DP832,456,2.0\n"])
    with patch(
        "scpi_psu_client.client.open_serial_connection", new_callable=AsyncMock, return_value=stream
    ):
        identity = await client.probe_identity()
        assert identity.raw_idn == "Rigol,DP832,456,2.0"
        assert identity.channels == 3
        assert client.profile.key == "standard"
    await client.close()


async def test_unknown_identity_recovery_does_not_accept_telemetry_as_marker() -> None:
    client = ScpiPsuClient("socket://bridge:4001", command_delay=0, write_delay=0)
    first = connection([TimeoutError()])
    recovery = connection(
        [b"54.190,1.635,88.600,OFF,OFF,OFF,1\n", b"5.455\n", (IDN + "\n").encode(), b"4.000\n"]
    )
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[first, recovery],
    ):
        assert await client.query("VOLT?") is None
        assert await client.query("VOLT?") == "4.000"
    await client.close()


async def test_boot_debug_text_flushed_on_reconnect_after_trip() -> None:
    """Verify boot text emitted by hardware power-cycle is purged during resync."""
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    first = connection([TimeoutError()])
    reboot_and_sync = connection(
        [
            b"[BOOT] Power supply init...\n",
            b"HW Rev: 2.1, Cal: OK\n",
            (IDN + "\n").encode(),
            b"12.345\n",
        ]
    )
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        side_effect=[first, reboot_and_sync],
    ):
        assert await client.query("VOLT?") is None
        assert not client.is_connected
        # Upon next query, reconnects, flushes buffer, resyncs via *IDN?, and VOLT? gets 12.345
        assert await client.query("VOLT?") == "12.345"
        assert client.is_connected
    await client.close()
