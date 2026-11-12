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
            b"main.c(879): CPU,CLK=120000000Hz\r\n",
            b"main.c(880): Normal startup,Table=368,TXT=1308\r\n",
            b"main.c(881): Date of build:(Apr 15 2025) Time: (14:06:24)\r\n",
            b"main.c(882): LCD ID=0xff858552,MADCTL=0x60\r\n",
            b"compensate.c(348):CURR ADC:1075838976,1610612736\r\n",
            (IDN + "\r\n").encode(),
            b"12.345\r\n",
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


async def test_boot_crud_and_empty_lines_during_normal_exchange() -> None:
    """Verify that stray empty lines and noise between queries do not invalidate session."""
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    conn = connection([b"\r\n", b"\n", b"12.345\r\n", b"\r\n", b"1.234\r\n"])
    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=conn,
    ):
        assert await client.query("VOLT?") == "12.345"
        assert await client.query("CURR?") == "1.234"
        assert client.is_connected
    await client.close()


async def test_midline_fragment_across_packets_reassembles_and_recovers() -> None:
    """Verify fragmented TCP packets delivering mid-line chunks are reassembled and buffered."""
    client = ScpiPsuClient("socket://bridge:4001", initial_idn=IDN, command_delay=0, write_delay=0)
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    with patch(
        "scpi_psu_client.client.open_serial_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ):
        await client.connect()
        # Feed initial mid-line chunk (segment 1) after connection established
        reader.feed_data(b"Abestop,AT6301,2532")
        # Now feed remaining chunk with terminator (segment 2)
        reader.feed_data(b"0554,FV:V6.0.0\r\n")
        ident = await client.probe_identity()
        assert ident.raw_idn == "Abestop,AT6301,25320554,FV:V6.0.0"
        assert ident.model == "AT6301"
        assert client.is_connected
    await client.close()
