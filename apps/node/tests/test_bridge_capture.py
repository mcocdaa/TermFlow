"""Bridge bounded pane capture handling (plan §9.2/§9.3, M2 A-side)."""

from uuid import UUID, uuid4

import pytest
from termflow_node.bridge.buffer import OutputBuffers
from termflow_node.bridge.runtime import BridgeRuntime
from termflow_node.tmux.capture import CaptureCommandError, RenderedCapture, truncate_tail
from termflow_protocol import (
    CaptureKind,
    CaptureRange,
    MessageType,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    PaneSnapshot,
    TopologySnapshot,
    ViewportGeometry,
    WindowSnapshot,
    WireMessage,
)


def topology(*, dead: bool = False) -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=dead,
                    )
                ],
            )
        ],
    )


class FakeTransport:
    def __init__(self, buffers: OutputBuffers) -> None:
        self.messages: list[WireMessage] = []
        self.buffers = buffers

    def enqueue_nowait(self, message: WireMessage) -> bool:
        if message.type is MessageType.PANE_OUTPUT:
            assert self.buffers.total_bytes > 0
        self.messages.append(message)
        return True


class RecordingControl:
    def __init__(self, rendered: bytes = b"rendered capture") -> None:
        self.rendered = rendered
        self.calls: list[dict[str, object]] = []

    async def capture_pane_bounded(
        self,
        pane_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        tail_lines: int | None = None,
        join_wrapped: bool = False,
        full_history: bool = False,
        max_bytes: int,
    ) -> RenderedCapture:
        self.calls.append(
            {
                "pane_id": pane_id,
                "start_line": start_line,
                "end_line": end_line,
                "tail_lines": tail_lines,
                "join_wrapped": join_wrapped,
                "full_history": full_history,
                "max_bytes": max_bytes,
            }
        )
        content, truncated = truncate_tail(self.rendered, max_bytes)
        return RenderedCapture(content=content, truncated=truncated)


class FakeInput:
    def retain_panes(self, pane_ids: set[str]) -> None:
        pass


def make_runtime(
    *,
    buffers: OutputBuffers | None = None,
    control: RecordingControl | None = None,
    dead: bool = False,
) -> tuple[BridgeRuntime, FakeTransport, UUID]:
    buffers = buffers or OutputBuffers(max_bytes_per_pane=1024)
    control = control or RecordingControl()
    transport = FakeTransport(buffers)
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=control,
        topology_provider=lambda: topology(dead=dead),
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    return runtime, transport, instance_id


def capture_request(instance_id: UUID, **overrides: object) -> PaneCaptureRequestPayload:
    defaults: dict[str, object] = {
        "instance_id": instance_id,
        "pane_id": "%1",
        "capture_kind": CaptureKind.VIEWPORT,
        "request_id": uuid4(),
        "max_bytes": 8192,
        "pane_incarnation": 1,
    }
    defaults.update(overrides)
    return PaneCaptureRequestPayload(**defaults)


async def deliver(
    runtime: BridgeRuntime,
    transport: FakeTransport,
    request: PaneCaptureRequestPayload,
) -> WireMessage:
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_CAPTURE_REQUEST,
            instance_id=request.instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    assert transport.messages
    return transport.messages[-1]


@pytest.mark.asyncio
async def test_viewport_capture_returns_result_with_echoed_request_id() -> None:
    runtime, transport, instance_id = make_runtime()
    buffers = runtime.buffers
    first = buffers.append("%1", b"one")
    buffers.append("%1", b"two")
    request = capture_request(instance_id)
    message = await deliver(runtime, transport, request)
    assert message.type is MessageType.PANE_CAPTURE_RESULT
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.request_id == request.request_id
    assert result.instance_id == instance_id
    assert result.pane_id == "%1"
    assert result.pane_incarnation == 1
    assert result.content == "rendered capture"
    assert result.encoding == "utf-8"
    assert result.stream_id == str(buffers.for_pane("%1").stream_id)
    assert result.from_seq == first.seq
    assert result.to_seq == 2
    assert result.capture_kind is CaptureKind.VIEWPORT
    assert result.capture_range is None
    assert result.viewport_geometry == ViewportGeometry(rows=24, cols=80)
    assert result.truncated is False
    assert result.stream_gap is False
    assert result.gap_reason is None
    assert result.result_code == "ok"
    call = runtime.control.calls[-1]
    assert call["max_bytes"] == 8192
    assert call["start_line"] is None
    assert call["end_line"] is None
    assert call["tail_lines"] is None
    assert call["full_history"] is False
    assert call["join_wrapped"] is False


@pytest.mark.asyncio
async def test_history_capture_applies_bounded_line_range() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        start_line=3,
        end_line=7,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.capture_range == CaptureRange(start=3, end=7)
    assert result.viewport_geometry is None
    call = runtime.control.calls[-1]
    assert call["start_line"] == 3
    assert call["end_line"] == 7
    assert call["full_history"] is False


@pytest.mark.asyncio
async def test_history_capture_applies_tail_and_join_wrapped() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        tail_lines=25,
        join_wrapped=True,
    )
    await deliver(runtime, transport, request)
    call = runtime.control.calls[-1]
    assert call["tail_lines"] == 25
    assert call["join_wrapped"] is True
    assert call["full_history"] is False


@pytest.mark.asyncio
async def test_history_capture_without_range_uses_full_history() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(instance_id, capture_kind=CaptureKind.HISTORY)
    await deliver(runtime, transport, request)
    assert runtime.control.calls[-1]["full_history"] is True


@pytest.mark.asyncio
async def test_snapshot_capture_uses_full_history_and_never_touches_live_ring() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    live = buffers.append("%1", b"live one")
    buffers.append("%1", b"live two")
    request = capture_request(instance_id, capture_kind=CaptureKind.SNAPSHOT)
    message = await deliver(runtime, transport, request)
    assert message.type is MessageType.PANE_CAPTURE_RESULT
    assert runtime.control.calls[-1]["full_history"] is True
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.stream_gap is False
    assert result.result_code == "ok"
    # The snapshot was served out-of-band: the live ring is untouched and no
    # PANE_OUTPUT was synthesized for it.
    assert buffers.for_pane("%1").stream_id == live.stream_id
    assert buffers.for_pane("%1").last_seq == 2
    assert buffers.total_bytes == len(b"live one") + len(b"live two")
    assert not any(m.type is MessageType.PANE_OUTPUT for m in transport.messages)


@pytest.mark.asyncio
async def test_gap_snapshot_reports_stream_gap_with_reason() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(instance_id, capture_kind=CaptureKind.GAP_SNAPSHOT)
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.stream_gap is True
    assert result.gap_reason == "stream_changed"
    assert result.result_code == "ok"
    assert runtime.control.calls[-1]["full_history"] is True


@pytest.mark.asyncio
async def test_gap_snapshot_derives_overwritten_when_cursor_was_evicted() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=4)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"abcd")
    buffers.append("%1", b"efgh")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.GAP_SNAPSHOT,
        stream_id=str(first.stream_id),
        seq=0,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.stream_gap is True
    assert result.gap_reason == "overwritten"


@pytest.mark.asyncio
async def test_since_capture_reads_ring_without_mutating_it() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"first\x1b[31m")
    buffers.append("%1", b"second")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=first.seq,
    )
    message = await deliver(runtime, transport, request)
    assert message.type is MessageType.PANE_CAPTURE_RESULT
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.content == "second"
    assert result.from_seq == 2
    assert result.to_seq == 2
    assert result.stream_id == str(first.stream_id)
    assert result.result_code == "ok"
    assert result.stream_gap is False
    assert result.capture_range is None
    # The raw stream chunks are served as-is and the ring is untouched.
    assert buffers.for_pane("%1").stream_id == first.stream_id
    assert buffers.for_pane("%1").last_seq == 2
    assert buffers.total_bytes == len(b"first\x1b[31m") + len(b"second")
    assert not any(m.type is MessageType.PANE_OUTPUT for m in transport.messages)


@pytest.mark.asyncio
async def test_since_capture_from_ring_start_returns_all_chunks() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"first")
    buffers.append("%1", b"second")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=0,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.content == "firstsecond"
    assert result.from_seq == 1
    assert result.to_seq == 2


@pytest.mark.asyncio
async def test_since_capture_truncates_to_max_bytes() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"a" * 100)
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=0,
        max_bytes=20,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.content == "a" * 20
    assert result.truncated is True
    assert result.from_seq == 1
    assert result.to_seq == 1


@pytest.mark.asyncio
async def test_since_capture_with_evicted_cursor_reports_stream_gap() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=4)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"abcd")
    buffers.append("%1", b"efgh")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=0,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.result_code == "stream_gap"
    assert result.stream_gap is True
    assert result.gap_reason == "overwritten"
    assert result.content == ""
    assert result.from_seq == 0
    assert result.to_seq == 0


@pytest.mark.asyncio
async def test_since_capture_with_unknown_stream_reports_stream_changed() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    buffers.append("%1", b"live")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(uuid4()),
        seq=1,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.stream_gap is True
    assert result.gap_reason == "stream_changed"
    assert result.result_code == "stream_gap"


@pytest.mark.asyncio
async def test_since_capture_without_ring_reports_stream_changed() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(uuid4()),
        seq=1,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.stream_gap is True
    assert result.gap_reason == "stream_changed"


@pytest.mark.asyncio
async def test_capture_rejects_unknown_pane() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(instance_id, pane_id="%404")
    message = await deliver(runtime, transport, request)
    assert message.type is MessageType.PANE_CAPTURE_ERROR
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.request_id == request.request_id
    assert error.instance_id == instance_id
    assert error.pane_id == "%404"
    assert error.error_code == "pane_not_found"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_dead_pane() -> None:
    runtime, transport, instance_id = make_runtime(dead=True)
    request = capture_request(instance_id)
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "pane_not_found"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_stale_pane_incarnation() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    buffers.append("%1", b"live")
    buffers.reset_stream("%1")
    request = capture_request(instance_id, pane_incarnation=1)
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "incarnation_changed"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_cursor_without_stream_id() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(instance_id, capture_kind=CaptureKind.HISTORY, seq=3)
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "invalid_request"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_cursor_without_seq() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(uuid4()),
    )
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "invalid_request"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_contradictory_line_selectors() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        tail_lines=10,
        start_line=2,
    )
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "invalid_request"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_ranges_for_snapshot_kind() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.SNAPSHOT,
        tail_lines=10,
    )
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "invalid_request"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_rejects_max_bytes_above_wire_cap() -> None:
    runtime, transport, instance_id = make_runtime()
    request = capture_request(instance_id, max_bytes=65_537)
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "quota_exceeded"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_capture_truncates_content_over_max_bytes() -> None:
    control = RecordingControl(rendered=b"x" * 200)
    runtime, transport, instance_id = make_runtime(control=control)
    request = capture_request(instance_id, max_bytes=50)
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.truncated is True
    assert len(result.content.encode("utf-8")) == 50


@pytest.mark.asyncio
async def test_capture_rejects_malformed_stream_id_as_invalid_request() -> None:
    runtime, transport, instance_id = make_runtime()
    buffers = runtime.buffers
    buffers.append("%1", b"live")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id="not-a-uuid",
        seq=1,
    )
    message = await deliver(runtime, transport, request)
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "invalid_request"
    assert runtime.control.calls == []


@pytest.mark.asyncio
async def test_large_history_capture_across_chunks_truncates_to_max_bytes() -> None:
    # A large history fed as many small chunks: the since-capture reassembles
    # the whole ring and truncates to the bounded tail, reporting the flag
    # (plan §19 M2: "very large history" and "bounded captures").
    buffers = OutputBuffers(max_bytes_per_pane=4096)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    raw = bytearray()
    first = None
    for index in range(30):
        chunk_data = f"line-{index:02d}:".encode() * 8  # 64 bytes per chunk
        chunk = buffers.append("%1", chunk_data)
        if first is None:
            first = chunk
        raw += chunk_data
    assert buffers.for_pane("%1").last_seq == 30
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=0,
        max_bytes=512,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.result_code == "ok"
    assert result.truncated is True
    assert len(result.content.encode("utf-8")) == 512
    assert result.content.encode("utf-8") == bytes(raw)[-512:]
    assert result.from_seq == 1
    assert result.to_seq == 30
    # The read never mutated the live ring and never synthesized output.
    ring = buffers.for_pane("%1")
    assert ring.stream_id == first.stream_id
    assert ring.last_seq == 30
    assert ring.total_bytes == len(raw)
    assert not any(message.type is MessageType.PANE_OUTPUT for message in transport.messages)


@pytest.mark.asyncio
async def test_split_escape_sequence_across_chunks_reassembles_coherently() -> None:
    # A CSI sequence split across chunk boundaries must survive: the ring
    # serves raw bytes and the since-capture reassembles them in order, so
    # the parser state on the reader never desyncs (plan §9.3).
    buffers = OutputBuffers(max_bytes_per_pane=4096)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    first = buffers.append("%1", b"\x1b[31")
    buffers.append("%1", b"mred")
    buffers.append("%1", b"\x1b[0")
    buffers.append("%1", b"m")
    request = capture_request(
        instance_id,
        capture_kind=CaptureKind.HISTORY,
        stream_id=str(first.stream_id),
        seq=0,
        max_bytes=4096,
    )
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.result_code == "ok"
    assert result.content == "\x1b[31mred\x1b[0m"
    assert result.truncated is False
    assert result.stream_gap is False
    # The ring is untouched: the raw chunks remain exactly as they were fed.
    replay = buffers.for_pane("%1").replay(first.stream_id, 0)
    assert [chunk.data for chunk in replay] == [b"\x1b[31", b"mred", b"\x1b[0", b"m"]


@pytest.mark.asyncio
async def test_capture_succeeds_with_current_incarnation_after_replacement() -> None:
    # Pane replacement: the old incarnation's cursor is rejected (covered by
    # test_capture_rejects_stale_pane_incarnation); a capture re-issued with
    # the current incarnation works against the fresh ring.
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    runtime, transport, instance_id = make_runtime(buffers=buffers)
    buffers.append("%1", b"old content")
    buffers.reset_stream("%1")
    assert buffers.pane_incarnation("%1") == 2
    request = capture_request(instance_id, pane_incarnation=2)
    message = await deliver(runtime, transport, request)
    result = PaneCaptureResultPayload.model_validate(message.payload)
    assert result.result_code == "ok"
    assert result.pane_incarnation == 2
    assert result.from_seq == 0
    assert result.to_seq == 0
    assert result.stream_gap is False


@pytest.mark.asyncio
async def test_capture_tmux_failure_returns_bounded_pane_error() -> None:
    class FailingControl(RecordingControl):
        async def capture_pane_bounded(
            self,
            pane_id: str,
            *,
            start_line: int | None = None,
            end_line: int | None = None,
            tail_lines: int | None = None,
            join_wrapped: bool = False,
            full_history: bool = False,
            max_bytes: int,
        ) -> RenderedCapture:
            raise CaptureCommandError(["tmux", "capture-pane", "-t", pane_id], 1)

    runtime, transport, instance_id = make_runtime(control=FailingControl())
    request = capture_request(instance_id)
    message = await deliver(runtime, transport, request)
    assert message.type is MessageType.PANE_CAPTURE_ERROR
    error = PaneCaptureErrorPayload.model_validate(message.payload)
    assert error.error_code == "pane_not_found"
    assert error.request_id == request.request_id
