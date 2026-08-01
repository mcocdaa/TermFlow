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
