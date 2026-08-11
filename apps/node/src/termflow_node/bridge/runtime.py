"""Composition of tmux observation, input, replay, capture, and Bridge transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from termflow_protocol import (
    CaptureKind,
    CaptureRange,
    CommandResultPayload,
    MessageType,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    PaneInputPayload,
    PaneOutputPayload,
    PaneReplayRequestPayload,
    PaneSnapshot,
    StreamGapPayload,
    TopologyChangedPayload,
    TopologySnapshot,
    ViewportGeometry,
    WireMessage,
    parse_payload,
)

from termflow_node.tmux.capture import (
    CaptureCommandError,
    RenderedCapture,
    truncate_tail,
)
from termflow_node.tmux.control_parser import (
    ControlNotification,
    GenericNotification,
    OutputNotification,
    PauseNotification,
)

from .buffer import OutputBuffers, OutputChunk, ReplayGap
from .terminal_manager import TerminalManager, is_terminal_runtime_message


class RuntimeControl(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def notifications(self) -> AsyncIterator[ControlNotification]: ...

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
    ) -> RenderedCapture: ...


class RuntimeTransport(Protocol):
    def enqueue_nowait(self, message: WireMessage) -> bool: ...

    async def run(
        self,
        handler: Callable[[WireMessage], Awaitable[None]],
        shutdown: asyncio.Event,
    ) -> None: ...


class RuntimeInputHandler(Protocol):
    async def handle(self, command: PaneInputPayload) -> CommandResultPayload: ...

    def retain_panes(self, pane_ids: set[str]) -> None: ...


_TOPOLOGY_EVENTS = {
    "window-add",
    "window-close",
    "window-renamed",
    "window-pane-changed",
    "session-window-changed",
    "layout-change",
}

# A-side ceiling for one bounded capture, matching the protocol wire cap for
# terminal payloads (MAX_TERMINAL_BYTES). Requests above it are rejected with
# quota_exceeded instead of being honored.
_MAX_CAPTURE_BYTES = 65_536


class BridgeRuntime:
    def __init__(
        self,
        *,
        instance_id: UUID,
        control: RuntimeControl,
        topology_provider: Callable[[], TopologySnapshot],
        transport: RuntimeTransport,
        buffers: OutputBuffers,
        input_handler: RuntimeInputHandler,
        topology_debounce_seconds: float = 0.05,
        terminal_manager: TerminalManager | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.control = control
        self.topology_provider = topology_provider
        self.transport = transport
        self.buffers = buffers
        self.input_handler = input_handler
        self._topology_debounce = topology_debounce_seconds
        self._topology: TopologySnapshot | None = None
        self.terminal_manager = terminal_manager

    def _message(self, message_type: MessageType, payload: dict[str, object]) -> WireMessage:
        return WireMessage(
            type=message_type,
            instance_id=self.instance_id,
            payload=payload,
        )

    def _publish_chunk(self, pane_id: str, chunk: OutputChunk) -> bool:
        payload = PaneOutputPayload(
            pane_id=pane_id,
            stream_id=chunk.stream_id,
            seq=chunk.seq,
            data_base64=PaneOutputPayload.from_bytes(
                pane_id,
                chunk.stream_id,
                chunk.seq,
                chunk.data,
            ).data_base64,
            captured_at=chunk.captured_at,
        )
        return self.transport.enqueue_nowait(
            self._message(MessageType.PANE_OUTPUT, payload.model_dump(mode="json"))
        )

    async def process_notification(self, notification: ControlNotification) -> None:
        if isinstance(notification, OutputNotification):
            chunk = self.buffers.append(notification.pane_id, notification.data)
            if not self._publish_chunk(notification.pane_id, chunk):
                self.buffers.reset_stream(notification.pane_id)
            return
        if isinstance(notification, PauseNotification) and notification.paused:
            await self._publish_gap(
                notification.pane_id,
                self.buffers.for_pane(notification.pane_id).stream_id,
                "control_paused",
            )
            return
        if isinstance(notification, GenericNotification) and notification.name in _TOPOLOGY_EVENTS:
            if self._topology_debounce:
                await asyncio.sleep(self._topology_debounce)
            await self._refresh_topology()

    async def _refresh_topology(self) -> None:
        topology = self.topology_provider()
        if self._topology == topology:
            return
        self._topology = topology
        pane_ids = {
            pane.pane_id
            for window in topology.windows
            for pane in window.panes
        }
        for pane_id in self.buffers.pane_ids - pane_ids:
            self.buffers.remove(pane_id)
        self.input_handler.retain_panes(pane_ids)
        payload = TopologyChangedPayload(topology=topology)
        self.transport.enqueue_nowait(
            self._message(MessageType.TOPOLOGY_CHANGED, payload.model_dump(mode="json"))
        )

    async def handle_message(self, message: WireMessage) -> None:
        if self.terminal_manager is not None and is_terminal_runtime_message(message.type):
            await self.terminal_manager.handle_wire_message(message)
            return
        payload = parse_payload(message.type, message.payload)
        if message.type is MessageType.PANE_INPUT:
            result = await self.input_handler.handle(cast(PaneInputPayload, payload))
            self.transport.enqueue_nowait(
                self._message(MessageType.COMMAND_RESULT, result.model_dump(mode="json"))
            )
        elif message.type is MessageType.PANE_REPLAY_REQUEST:
            await self._handle_replay(cast(PaneReplayRequestPayload, payload))
        elif message.type is MessageType.PANE_CAPTURE_REQUEST:
            await self._handle_capture(cast(PaneCaptureRequestPayload, payload))

    async def _handle_replay(self, request: PaneReplayRequestPayload) -> None:
        topology = self.topology_provider()
        if not topology.contains_pane(request.pane_id):
            return
        replay = self.buffers.replay(request.pane_id, request.stream_id, request.after_seq)
        if isinstance(replay, ReplayGap):
            await self._publish_gap(
                request.pane_id,
                request.stream_id,
                replay.reason,
            )
            return
        for chunk in replay:
            if not self._publish_chunk(request.pane_id, chunk):
                self.buffers.reset_stream(request.pane_id)
                return

    def _pane(self, pane_id: str) -> PaneSnapshot | None:
        topology = self.topology_provider()
        return next(
            (
                pane
                for window in topology.windows
                for pane in window.panes
                if pane.pane_id == pane_id
            ),
            None,
        )

    def _send_capture_error(
        self,
        request: PaneCaptureRequestPayload,
        error_code: str,
        message: str,
    ) -> None:
        payload = PaneCaptureErrorPayload(
            request_id=request.request_id,
            instance_id=self.instance_id,
            pane_id=request.pane_id,
            error_code=error_code,
            message=message,
        )
        self.transport.enqueue_nowait(
            self._message(MessageType.PANE_CAPTURE_ERROR, payload.model_dump(mode="json"))
        )

    def _send_capture_result(self, result: PaneCaptureResultPayload) -> None:
        self.transport.enqueue_nowait(
            self._message(MessageType.PANE_CAPTURE_RESULT, result.model_dump(mode="json"))
        )

    async def _handle_capture(self, request: PaneCaptureRequestPayload) -> None:
        pane = self._pane(request.pane_id)
        if pane is None or pane.dead:
            self._send_capture_error(
                request,
                "pane_not_found",
                f"pane {request.pane_id} is not present or is dead",
            )
            return
        if request.pane_incarnation != self.buffers.pane_incarnation(request.pane_id):
            self._send_capture_error(
                request,
                "incarnation_changed",
                "the pane incarnation no longer matches this request",
            )
            return
        if request.max_bytes > _MAX_CAPTURE_BYTES:
            self._send_capture_error(
                request,
                "quota_exceeded",
                f"max_bytes exceeds the {_MAX_CAPTURE_BYTES} byte capture ceiling",
            )
            return
        try:
            result = await self._build_capture_result(request, pane)
        except ValueError as exc:
            self._send_capture_error(request, "invalid_request", str(exc))
            return
        except CaptureCommandError:
            self._send_capture_error(
                request,
                "pane_not_found",
                "tmux could not render the pane",
            )
            return
        self._send_capture_result(result)

    async def _build_capture_result(
        self,
        request: PaneCaptureRequestPayload,
        pane: PaneSnapshot,
    ) -> PaneCaptureResultPayload:
        kind = request.capture_kind
        if kind is CaptureKind.HISTORY and (
            request.stream_id is not None or request.seq is not None
        ):
            # ``view=since``: serve raw chunks after the cursor straight from
            # the buffered ring. The ring is read-only here; a snapshot is
            # never appended into it (§9.2).
            if request.stream_id is None or request.seq is None:
                raise ValueError(
                    "history reads addressed by a cursor require both stream_id and seq"
                )
            return self._capture_since(request, stream_id=request.stream_id, after_seq=request.seq)
        if kind is CaptureKind.VIEWPORT:
            rendered = await self.control.capture_pane_bounded(
                request.pane_id,
                join_wrapped=request.join_wrapped,
                max_bytes=request.max_bytes,
            )
            return self._rendered_result(
                request,
                rendered,
                capture_range=None,
                viewport_geometry=ViewportGeometry(rows=pane.height, cols=pane.width),
                stream_gap=False,
                gap_reason=None,
            )
        if kind in {CaptureKind.SNAPSHOT, CaptureKind.GAP_SNAPSHOT}:
            if (
                request.start_line is not None
                or request.end_line is not None
                or request.tail_lines is not None
            ):
                raise ValueError("snapshot captures do not accept line ranges")
            rendered = await self.control.capture_pane_bounded(
                request.pane_id,
                join_wrapped=request.join_wrapped,
                full_history=True,
                max_bytes=request.max_bytes,
            )
            return self._rendered_result(
                request,
                rendered,
                capture_range=None,
                viewport_geometry=None,
                stream_gap=kind is CaptureKind.GAP_SNAPSHOT,
                gap_reason=self._gap_reason(request) if kind is CaptureKind.GAP_SNAPSHOT else None,
            )
        if request.tail_lines is not None and (
            request.start_line is not None or request.end_line is not None
        ):
            raise ValueError("tail_lines cannot be combined with start_line or end_line")
        full_history = (
            request.start_line is None
            and request.end_line is None
            and request.tail_lines is None
        )
        rendered = await self.control.capture_pane_bounded(
            request.pane_id,
            start_line=request.start_line,
            end_line=request.end_line,
            tail_lines=request.tail_lines,
            join_wrapped=request.join_wrapped,
            full_history=full_history,
            max_bytes=request.max_bytes,
        )
        start_line = request.start_line
        end_line = request.end_line
        capture_range = (
            CaptureRange(start=start_line, end=end_line)
            if start_line is not None and end_line is not None
            else None
        )
        return self._rendered_result(
            request,
            rendered,
            capture_range=capture_range,
            viewport_geometry=None,
            stream_gap=False,
            gap_reason=None,
        )

    def _stream_bounds(self, pane_id: str) -> tuple[str, int, int]:
        """(stream_id, from_seq, to_seq) of the pane's live stream.

        With no retained ring the result addresses a fresh marker stream at
        position 0; live output then starts a new stream that the reader can
        detect by stream mismatch.
        """
        buffer = self.buffers.peek(pane_id)
        if buffer is None:
            return (str(uuid4()), 0, 0)
        last = buffer.last_seq
        first = buffer.first_seq if buffer.first_seq is not None else last
        return (str(buffer.stream_id), first, last)

    def _rendered_result(
        self,
        request: PaneCaptureRequestPayload,
        rendered: RenderedCapture,
        *,
        capture_range: CaptureRange | None,
        viewport_geometry: ViewportGeometry | None,
        stream_gap: bool,
        gap_reason: str | None,
    ) -> PaneCaptureResultPayload:
        stream_id, from_seq, to_seq = self._stream_bounds(request.pane_id)
        return PaneCaptureResultPayload(
            request_id=request.request_id,
            instance_id=self.instance_id,
            pane_id=request.pane_id,
            pane_incarnation=request.pane_incarnation,
            content=rendered.content.decode("utf-8", errors="replace"),
            encoding="utf-8",
            stream_id=stream_id,
            from_seq=from_seq,
            to_seq=to_seq,
            capture_kind=request.capture_kind,
            capture_range=capture_range,
            viewport_geometry=viewport_geometry,
            truncated=rendered.truncated,
            stream_gap=stream_gap,
            gap_reason=gap_reason,
            result_code="ok",
        )

    def _gap_reason(self, request: PaneCaptureRequestPayload) -> str:
        """Derive the gap reason for a gap_snapshot against the live ring."""
        if request.stream_id is None:
            return "stream_changed"
        buffer = self.buffers.peek(request.pane_id)
        if buffer is None:
            return "stream_changed"
        replay = buffer.replay(UUID(request.stream_id), request.seq or 0)
        if isinstance(replay, ReplayGap):
            return replay.reason
        return "stream_changed"

    def _capture_since(
        self,
        request: PaneCaptureRequestPayload,
        *,
        stream_id: str,
        after_seq: int,
    ) -> PaneCaptureResultPayload:
        """Serve raw ring chunks after a cursor; never mutates the ring.

        ``from_seq``/``to_seq`` always describe the full chunk range that was
        read; ``max_bytes`` truncation keeps the newest tail, so the reader
        resumes from ``to_seq``.
        """
        buffer = self.buffers.peek(request.pane_id)
        if buffer is None:
            return self._gap_result(request, "stream_changed", after_seq)
        replay = buffer.replay(UUID(stream_id), after_seq)
        if isinstance(replay, ReplayGap):
            return self._gap_result(request, replay.reason, after_seq)
        raw = b"".join(chunk.data for chunk in replay)
        content, truncated = truncate_tail(raw, request.max_bytes)
        from_seq = replay[0].seq if replay else after_seq
        to_seq = replay[-1].seq if replay else after_seq
        return PaneCaptureResultPayload(
            request_id=request.request_id,
            instance_id=self.instance_id,
            pane_id=request.pane_id,
            pane_incarnation=request.pane_incarnation,
            content=content.decode("utf-8", errors="replace"),
            encoding="utf-8",
            stream_id=stream_id,
            from_seq=from_seq,
            to_seq=to_seq,
            capture_kind=request.capture_kind,
            capture_range=None,
            viewport_geometry=None,
            truncated=truncated,
            stream_gap=False,
            gap_reason=None,
            result_code="ok",
        )

    def _gap_result(
        self,
        request: PaneCaptureRequestPayload,
        reason: str,
        after_seq: int,
    ) -> PaneCaptureResultPayload:
        return PaneCaptureResultPayload(
            request_id=request.request_id,
            instance_id=self.instance_id,
            pane_id=request.pane_id,
            pane_incarnation=request.pane_incarnation,
            content="",
            encoding="utf-8",
            stream_id=str(request.stream_id) if request.stream_id is not None else "",
            from_seq=after_seq,
            to_seq=after_seq,
            capture_kind=request.capture_kind,
            capture_range=None,
            viewport_geometry=None,
            truncated=False,
            stream_gap=True,
            gap_reason=reason,
            result_code="stream_gap",
        )

    async def _publish_gap(
        self,
        pane_id: str,
        previous_stream_id: UUID,
        reason: Literal["stream_changed", "overwritten", "backpressure", "control_paused"],
    ) -> None:
        """Publish a stream gap and reset the live ring to a fresh stream.

        Snapshot/live separation (§9.2): a rendered snapshot is NOT appended
        into the live output ring as though it were new terminal output. The
        peer reconciles the gap through explicit bounded capture requests
        (``snapshot``/``gap_snapshot`` kinds) against the fresh stream.
        """
        gap = StreamGapPayload(
            pane_id=pane_id,
            previous_stream_id=previous_stream_id,
            reason=reason,
        )
        self.transport.enqueue_nowait(
            self._message(MessageType.STREAM_GAP, gap.model_dump(mode="json"))
        )
        self.buffers.reset_stream(pane_id)

    async def _control_loop(self, shutdown: asyncio.Event) -> None:
        async for notification in self.control.notifications():
            await self.process_notification(notification)
        shutdown.set()

    async def run(self, shutdown: asyncio.Event) -> None:
        await self.control.start()
        self._topology = self.topology_provider()
        tasks = {
            asyncio.create_task(self.transport.run(self.handle_message, shutdown)),
            asyncio.create_task(self._control_loop(shutdown)),
            asyncio.create_task(shutdown.wait()),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            if self.terminal_manager is not None:
                await self.terminal_manager.close()
            await self.control.close()
