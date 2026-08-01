"""Bounded, history-free routing state for browser terminal owners."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from uuid import UUID, uuid4

from termflow_protocol import (
    MessageType,
    TerminalCloseReason,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    WireMessage,
)


@dataclass(frozen=True, slots=True)
class LocalTerminalClose:
    reason: TerminalCloseReason
    error_code: str | None = None


class BrowserTerminal:
    """One browser owner with a byte/count bounded A-to-C queue."""

    def __init__(
        self,
        *,
        instance_id: UUID,
        session_key: str | None,
        queue_max_messages: int,
        queue_max_bytes: int,
    ) -> None:
        self.instance_id = instance_id
        self.terminal_id = uuid4()
        self.session_key = session_key
        self._queue_max_messages = queue_max_messages
        self._queue_max_bytes = queue_max_bytes
        self._queue: deque[tuple[WireMessage, int]] = deque()
        self._queued_bytes = 0
        self._available = asyncio.Event()
        self._local_close: LocalTerminalClose | None = None
        self._stream_id: UUID | None = None
        self._last_seq = 0
        self.close_requested = False
        self.remote_closed = False

    @property
    def terminated(self) -> bool:
        return self._local_close is not None

    @property
    def resume_cursor(self) -> tuple[UUID, int] | None:
        if self._stream_id is None:
            return None
        return self._stream_id, self._last_seq

    def terminate(
        self,
        reason: TerminalCloseReason,
        *,
        error_code: str | None = None,
    ) -> None:
        if self._local_close is None:
            self._local_close = LocalTerminalClose(reason, error_code)
            self._available.set()

    def enqueue(self, message: WireMessage, *, byte_count: int = 0) -> bool:
        if self.terminated:
            return False
        if (
            len(self._queue) >= self._queue_max_messages
            or self._queued_bytes + byte_count > self._queue_max_bytes
        ):
            self.terminate("internal_error", error_code="backpressure")
            return False
        self._queue.append((message, byte_count))
        self._queued_bytes += byte_count
        self._available.set()
        return True

    async def next_event(self) -> WireMessage | LocalTerminalClose:
        while True:
            if self._local_close is not None:
                return self._local_close
            if self._queue:
                message, byte_count = self._queue.popleft()
                self._queued_bytes -= byte_count
                if not self._queue:
                    self._available.clear()
                return message
            self._available.clear()
            await self._available.wait()

    def observe_opened(self, opened: TerminalOpenedPayload) -> None:
        if self._stream_id != opened.stream_id:
            self._stream_id = opened.stream_id
            self._last_seq = 0

    def accepts_output(self, output: TerminalOutputPayload) -> bool:
        return (
            self._stream_id == output.stream_id
            and output.seq == self._last_seq + 1
        )

    def observe_output(self, output: TerminalOutputPayload) -> None:
        self._last_seq = output.seq

    def prepare_resume(self, stream_id: UUID, after_seq: int) -> bool:
        if (
            self.terminated
            or self.remote_closed
            or self._stream_id != stream_id
            or after_seq < 0
            or after_seq > self._last_seq
        ):
            return False
        self._queue.clear()
        self._queued_bytes = 0
        self._available.clear()
        self._last_seq = after_seq
        return True


class TerminalHub:
    def __init__(self, *, queue_max_messages: int, queue_max_bytes: int) -> None:
        if queue_max_messages < 1 or queue_max_bytes < 1:
            raise ValueError("terminal queue bounds must be positive")
        self._queue_max_messages = queue_max_messages
        self._queue_max_bytes = queue_max_bytes
        self._current: dict[UUID, BrowserTerminal] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        instance_id: UUID,
        *,
        session_key: str | None,
    ) -> BrowserTerminal:
        terminal = BrowserTerminal(
            instance_id=instance_id,
            session_key=session_key,
            queue_max_messages=self._queue_max_messages,
            queue_max_bytes=self._queue_max_bytes,
        )
        async with self._lock:
            previous = self._current.get(instance_id)
            self._current[instance_id] = terminal
            if previous is not None:
                previous.terminate("replaced")
        return terminal

    async def unregister(self, terminal: BrowserTerminal) -> bool:
        async with self._lock:
            if self._current.get(terminal.instance_id) is not terminal:
                return False
            del self._current[terminal.instance_id]
            return True

    async def resume(
        self,
        instance_id: UUID,
        *,
        session_key: str | None,
        terminal_id: UUID,
        stream_id: UUID,
        after_seq: int,
    ) -> BrowserTerminal | None:
        async with self._lock:
            terminal = self._current.get(instance_id)
            if (
                terminal is None
                or terminal.terminal_id != terminal_id
                or terminal.session_key != session_key
                or not terminal.prepare_resume(stream_id, after_seq)
            ):
                return None
            return terminal

    def abandon(self, terminal: BrowserTerminal) -> bool:
        """Cancellation-safe cleanup for a WebSocket task's synchronous finally block."""

        if self._current.get(terminal.instance_id) is not terminal:
            return False
        del self._current[terminal.instance_id]
        return True

    async def current(self, instance_id: UUID) -> BrowserTerminal | None:
        async with self._lock:
            return self._current.get(instance_id)

    async def forward(self, message: WireMessage) -> bool:
        terminal = await self.current(message.instance_id)
        if terminal is None:
            return False
        raw_terminal_id = message.payload.get("terminal_id")
        if str(raw_terminal_id) != str(terminal.terminal_id):
            return False

        byte_count = 0
        if message.type is MessageType.TERMINAL_OPENED:
            opened = TerminalOpenedPayload.model_validate(message.payload)
            terminal.observe_opened(opened)
        elif message.type is MessageType.TERMINAL_OUTPUT:
            output = TerminalOutputPayload.model_validate(message.payload)
            if not terminal.accepts_output(output):
                terminal.terminate("stream_gap", error_code="stream_gap")
                return False
            byte_count = len(output.to_bytes())
            if not terminal.enqueue(message, byte_count=byte_count):
                return False
            terminal.observe_output(output)
            return True
        elif message.type is MessageType.TERMINAL_CLOSED:
            terminal.remote_closed = True
        return terminal.enqueue(message, byte_count=byte_count)

    async def terminate_session(self, session_key: str) -> int:
        async with self._lock:
            matches = [
                terminal
                for terminal in self._current.values()
                if terminal.session_key == session_key
            ]
        for terminal in matches:
            terminal.terminate("client_closed")
        return len(matches)
