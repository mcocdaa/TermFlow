from uuid import uuid4

from termflow_node.bridge.buffer import OutputBuffers, PaneOutputBuffer, ReplayGap


def test_buffer_replays_by_stream_and_sequence() -> None:
    buffer = PaneOutputBuffer(max_bytes=8)
    first = buffer.append(b"abc")
    second = buffer.append(b"de")
    replay = buffer.replay(first.stream_id, first.seq)
    assert not isinstance(replay, ReplayGap)
    assert [chunk.data for chunk in replay] == [b"de"]
    assert second.seq == first.seq + 1


def test_overwrite_reports_gap() -> None:
    buffer = PaneOutputBuffer(max_bytes=4)
    old = buffer.append(b"abc")
    buffer.append(b"def")
    gap = buffer.replay(old.stream_id, 0)
    assert gap == ReplayGap(reason="overwritten")


def test_oversized_chunk_keeps_only_tail_and_marks_chunk_unreplayable() -> None:
    buffer = PaneOutputBuffer(max_bytes=4)
    chunk = buffer.append(b"abcdef")
    assert chunk.data == b"cdef"
    assert buffer.total_bytes == 4
    assert buffer.replay(chunk.stream_id, chunk.seq - 1) == ReplayGap(reason="overwritten")


def test_registry_releases_bytes_and_resets_stream() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=8)
    first = buffers.append("%1", b"abc")
    buffers.append("%2", b"de")
    assert buffers.total_bytes == 5
    buffers.remove("%2")
    assert buffers.total_bytes == 3
    new_stream = buffers.reset_stream("%1")
    assert new_stream != first.stream_id
    assert buffers.total_bytes == 0
    assert buffers.replay("%404", uuid4(), 0) == ReplayGap(reason="stream_changed")


def test_buffer_exposes_first_and_last_seq() -> None:
    buffer = PaneOutputBuffer(max_bytes=64)
    assert buffer.first_seq is None
    assert buffer.last_seq == 0
    buffer.append(b"a")
    buffer.append(b"b")
    assert buffer.first_seq == 1
    assert buffer.last_seq == 2


def test_registry_peek_does_not_create_a_buffer() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=8)
    assert buffers.peek("%1") is None
    assert "%1" not in buffers.pane_ids
    buffers.append("%1", b"abc")
    assert buffers.peek("%1") is buffers.for_pane("%1")


def test_registry_pane_incarnation_starts_at_one_and_bumps_on_stream_reset() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=8)
    assert buffers.pane_incarnation("%1") == 1
    buffers.append("%1", b"abc")
    assert buffers.pane_incarnation("%1") == 1
    buffers.reset_stream("%1")
    assert buffers.pane_incarnation("%1") == 2
    buffers.reset_stream("%1")
    assert buffers.pane_incarnation("%1") == 3


def test_registry_pane_incarnation_bumps_when_pane_is_removed() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=8)
    buffers.append("%1", b"abc")
    buffers.remove("%1")
    assert buffers.pane_incarnation("%1") == 2
