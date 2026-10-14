import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from termflow_node.tmux.control_client import CONTROL_STREAM_LIMIT, TmuxControlClient
from termflow_node.tmux.control_parser import (
    GenericNotification,
    MalformedControlNotification,
    OutputNotification,
    parse_control_line,
)


def test_output_notification_preserves_arbitrary_bytes() -> None:
    event = parse_control_line(b"%output %7 hi\\015\\012\\033[31m\\134x\n")
    assert event == OutputNotification("%7", b"hi\r\n\x1b[31m\\x")


def test_non_output_notification_remains_structured() -> None:
    event = parse_control_line(b"%window-add @3\n")
    assert event == GenericNotification(name="window-add", arguments=("@3",))


def test_extended_output_ignores_flow_control_metadata() -> None:
    event = parse_control_line(b"%extended-output %2 15 future : hi\\015\\012\n")
    assert event == OutputNotification("%2", b"hi\r\n")


@pytest.mark.parametrize("line", [b"%output %1 bad\\x\n", b"%output %1 bad\\12\n"])
def test_malformed_output_escape_is_rejected(line: bytes) -> None:
    with pytest.raises(MalformedControlNotification):
        parse_control_line(line)


@pytest.mark.asyncio
async def test_control_client_handles_lines_over_64kib_and_drains_overrun_chunks() -> None:
    assert CONTROL_STREAM_LIMIT >= 4 * 1024 * 1024

    # 1. Verify a >64 KiB line succeeds under the 4 MiB stream limit
    reader_large = asyncio.StreamReader(limit=CONTROL_STREAM_LIMIT)
    large_payload = b"x" * 100_000
    reader_large.feed_data(b"%output %1 " + large_payload + b"\n")
    reader_large.feed_eof()
    client_large = TmuxControlClient(Path("/tmp/fake.sock"), "main")
    client_large._process = SimpleNamespace(stdout=reader_large)  # type: ignore[assignment]
    events_large = [item async for item in client_large.notifications()]
    assert events_large == [OutputNotification("%1", large_payload)]

    # 2. Verify a chunk exceeding the stream limit is drained without crashing notifications()
    reader_small = asyncio.StreamReader(limit=64)
    # Chunk without newline exceeding limit, followed by tail with newline, followed by valid line
    reader_small.feed_data(b"%output %1 " + b"a" * 100)
    client_small = TmuxControlClient(Path("/tmp/fake.sock"), "main", stream_limit=64)
    client_small._process = SimpleNamespace(stdout=reader_small)  # type: ignore[assignment]

    async def feed_rest() -> None:
        await asyncio.sleep(0.01)
        reader_small.feed_data(b"tail\n%output %1 recovered\n")
        reader_small.feed_eof()

    feeder = asyncio.create_task(feed_rest())
    events_small = [item async for item in client_small.notifications()]
    await feeder
    assert events_small == [OutputNotification("%1", b"recovered")]
