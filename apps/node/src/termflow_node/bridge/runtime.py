"""Composition of tmux observation, input, replay, and Bridge transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Literal, Protocol, cast
from uuid import UUID

from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    PaneInputPayload,
    PaneOutputPayload,
    PaneReplayRequestPayload,
    StreamGapPayload,
    TopologyChangedPayload,
    TopologySnapshot,
    WireMessage,
    parse_payload,
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

    async def capture_pane(self, pane_id: str) -> bytes: ...


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
            await self._publish_gap_and_snapshot(
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

    async def _handle_replay(self, request: PaneReplayRequestPayload) -> None:
        topology = self.topology_provider()
        if not topology.contains_pane(request.pane_id):
            return
        replay = self.buffers.replay(request.pane_id, request.stream_id, request.after_seq)
        if isinstance(replay, ReplayGap):
            await self._publish_gap_and_snapshot(
                request.pane_id,
                request.stream_id,
                replay.reason,
            )
            return
        for chunk in replay:
            if not self._publish_chunk(request.pane_id, chunk):
                self.buffers.reset_stream(request.pane_id)
                return

    async def _publish_gap_and_snapshot(
        self,
        pane_id: str,
        previous_stream_id: UUID,
        reason: Literal["stream_changed", "overwritten", "backpressure", "control_paused"],
    ) -> None:
        gap = StreamGapPayload(
            pane_id=pane_id,
            previous_stream_id=previous_stream_id,
            reason=reason,
        )
        self.transport.enqueue_nowait(
            self._message(MessageType.STREAM_GAP, gap.model_dump(mode="json"))
        )
        snapshot = await self.control.capture_pane(pane_id)
        self.buffers.reset_stream(pane_id)
        chunk = self.buffers.append(pane_id, snapshot)
        self._publish_chunk(pane_id, chunk)

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
