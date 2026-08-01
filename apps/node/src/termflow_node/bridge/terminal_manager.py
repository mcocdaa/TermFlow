"""One-owner remote tmux terminal lifecycle retained across Bridge reconnects."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError
from termflow_protocol import (
    MessageType,
    TerminalActionPayload,
    TerminalActionResultPayload,
    TerminalBindingsPayload,
    TerminalClosedPayload,
    TerminalClosePayload,
    TerminalCloseReason,
    TerminalInputPayload,
    TerminalOpenedPayload,
    TerminalOpenPayload,
    TerminalOutputPayload,
    TerminalSizePayload,
    TermRenamePayload,
    TermRenameResultPayload,
    TopologyChangedPayload,
    TopologySnapshot,
    WireMessage,
    parse_payload,
)

from termflow_node.tmux.actions import ActionRejected
from termflow_node.tmux.client_size import ClientSizeResolver, TerminalSize
from termflow_node.tmux.remote_client import RemoteOutputChunk, RemoteTmuxClient, ReplayGap
from termflow_node.tmux.runner import TmuxClient


class ManagedRemote(Protocol):
    terminal_id: UUID
    stream_id: UUID
    slave_tty: str | None

    async def start(self) -> None: ...

    async def wait_ready(self, *, wait_seconds: float = 5.0) -> None: ...

    async def write(self, data: bytes) -> None: ...

    def resize(self, rows: int, cols: int) -> None: ...

    def replay_after(self, seq: int) -> list[RemoteOutputChunk]: ...

    async def close(self) -> None: ...


class ManagerRunner(Protocol):
    def list_clients(self, session_id: str) -> list[TmuxClient]: ...


class ActionExecutor(Protocol):
    def execute(self, action: TerminalActionPayload) -> None: ...


class BindingReader(Protocol):
    def read(self, terminal_id: UUID) -> TerminalBindingsPayload: ...


class Renamer(Protocol):
    def rename(self, name: str) -> TopologySnapshot: ...


RemoteFactory = Callable[..., ManagedRemote]
Publisher = Callable[[WireMessage], bool]

_TERMINAL_TYPES = {
    MessageType.TERMINAL_OPEN,
    MessageType.TERMINAL_INPUT,
    MessageType.TERMINAL_ACTION,
    MessageType.TERMINAL_CLOSE,
    MessageType.TERMINAL_SIZE,
    MessageType.TERMINAL_OPENED,
    MessageType.TERMINAL_OUTPUT,
    MessageType.TERMINAL_BINDINGS,
    MessageType.TERMINAL_ACTION_RESULT,
    MessageType.TERMINAL_CLOSED,
}


def is_terminal_runtime_message(message_type: MessageType) -> bool:
    return message_type in _TERMINAL_TYPES or message_type is MessageType.TERM_RENAME


@dataclass(slots=True)
class _CurrentTerminal:
    remote: ManagedRemote
    size: TerminalSize
    size_task: asyncio.Task[None] | None = None
    opened_announced: bool = False
    startup_output: list[RemoteOutputChunk] = field(default_factory=list)


class TerminalManager:
    def __init__(
        self,
        *,
        instance_id: UUID,
        socket_path: Path,
        session_id: str,
        runner: ManagerRunner,
        topology_provider: Callable[[], TopologySnapshot],
        publish: Publisher,
        action_executor: ActionExecutor,
        binding_reader: BindingReader,
        renamer: Renamer | None = None,
        remote_factory: RemoteFactory | None = None,
        creation_size: TerminalSize | None = None,
        grace_seconds: float = 30.0,
        size_poll_seconds: float = 0.5,
    ) -> None:
        self.instance_id = instance_id
        self._socket_path = socket_path
        self._session_id = session_id
        self._runner = runner
        self._topology_provider = topology_provider
        self._publish = publish
        self._action_executor = action_executor
        self._binding_reader = binding_reader
        self._renamer = renamer
        self._remote_factory = remote_factory or cast(RemoteFactory, RemoteTmuxClient)
        self._size_resolver = ClientSizeResolver(
            runner,
            session_id,
            creation_size=creation_size,
        )
        self._grace_seconds = grace_seconds
        self._size_poll_seconds = size_poll_seconds
        self._current: _CurrentTerminal | None = None
        self._grace_task: asyncio.Task[None] | None = None
        self._bridge_is_connected = True
        self._lock = asyncio.Lock()

    @property
    def current_terminal_id(self) -> UUID | None:
        return self._current.remote.terminal_id if self._current is not None else None

    def _message(self, message_type: MessageType, payload: BaseModel) -> WireMessage:
        return WireMessage(
            type=message_type,
            instance_id=self.instance_id,
            payload=payload.model_dump(mode="json"),
        )

    def _send(self, message_type: MessageType, payload: BaseModel) -> bool:
        if not self._bridge_is_connected:
            return False
        return self._publish(self._message(message_type, payload))

    def _send_topology(self, topology: TopologySnapshot | None = None) -> None:
        payload = TopologyChangedPayload(topology=topology or self._topology_provider())
        self._send(MessageType.TOPOLOGY_CHANGED, payload)

    def _cancel_grace(self) -> None:
        task = self._grace_task
        self._grace_task = None
        if task is not None:
            task.cancel()

    def bridge_connected(self) -> None:
        self._bridge_is_connected = True

    def bridge_disconnected(self) -> None:
        self._bridge_is_connected = False
        self._cancel_grace()
        if self._current is not None:
            self._grace_task = asyncio.create_task(self._expire_after_grace())

    async def _expire_after_grace(self) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
            async with self._lock:
                if self._current is not None:
                    await self._close_current("grace_expired")
        except asyncio.CancelledError:
            raise

    async def _on_output(self, chunk: RemoteOutputChunk) -> None:
        current = self._current
        if (
            current is None
            or current.remote.terminal_id != chunk.terminal_id
            or current.remote.stream_id != chunk.stream_id
        ):
            return
        if not current.opened_announced:
            current.startup_output.append(chunk)
            return
        self._publish_output(chunk)

    def _publish_output(self, chunk: RemoteOutputChunk) -> None:
        payload = TerminalOutputPayload.from_bytes(
            chunk.terminal_id,
            chunk.stream_id,
            chunk.seq,
            chunk.data,
        )
        self._send(MessageType.TERMINAL_OUTPUT, payload)

    async def _on_remote_closed(self, terminal_id: UUID, reason: str) -> None:
        current = self._current
        if current is None or current.remote.terminal_id != terminal_id:
            return
        self._current = None
        if current.size_task is not None:
            current.size_task.cancel()
        close_reason: TerminalCloseReason = (
            "client_closed" if reason == "client_closed" else "internal_error"
        )
        self._send(
            MessageType.TERMINAL_CLOSED,
            TerminalClosedPayload(terminal_id=terminal_id, reason=close_reason),
        )

    async def _wait_attached(self, remote: ManagedRemote) -> None:
        await remote.wait_ready(wait_seconds=5.0)
        slave_tty = remote.slave_tty
        if slave_tty is None:
            return
        for _ in range(100):
            if any(
                client.tty == slave_tty
                for client in self._runner.list_clients(self._session_id)
            ):
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("remote tmux client did not attach")

    async def _open_fresh(self, terminal_id: UUID) -> None:
        size = self._size_resolver.resolve(proxy_ttys=set())

        async def closed(reason: str) -> None:
            await self._on_remote_closed(terminal_id, reason)

        remote = self._remote_factory(
            terminal_id=terminal_id,
            socket_path=self._socket_path,
            session_id=self._session_id,
            rows=size.rows,
            cols=size.cols,
            on_output=self._on_output,
            on_closed=closed,
        )
        current = _CurrentTerminal(remote=remote, size=size)
        self._current = current
        try:
            await remote.start()
            await self._wait_attached(remote)
        except BaseException:
            self._current = None
            await remote.close()
            raise
        self._send_opened_and_bindings(current)
        current.opened_announced = True
        for chunk in current.startup_output:
            self._publish_output(chunk)
        current.startup_output.clear()
        current.size_task = asyncio.create_task(self._size_loop(current))

    def _send_opened_and_bindings(self, current: _CurrentTerminal) -> None:
        remote = current.remote
        self._send(
            MessageType.TERMINAL_OPENED,
            TerminalOpenedPayload(
                terminal_id=remote.terminal_id,
                stream_id=remote.stream_id,
                rows=current.size.rows,
                cols=current.size.cols,
            ),
        )
        self._send(MessageType.TERMINAL_BINDINGS, self._binding_reader.read(remote.terminal_id))

    async def _size_loop(self, expected: _CurrentTerminal) -> None:
        try:
            while self._current is expected:
                await asyncio.sleep(self._size_poll_seconds)
                proxy_tty = expected.remote.slave_tty
                size = self._size_resolver.resolve(
                    proxy_ttys={proxy_tty} if proxy_tty is not None else set()
                )
                if size == expected.size:
                    continue
                expected.remote.resize(size.rows, size.cols)
                expected.size = size
                self._send(
                    MessageType.TERMINAL_SIZE,
                    TerminalSizePayload(
                        terminal_id=expected.remote.terminal_id,
                        rows=size.rows,
                        cols=size.cols,
                    ),
                )
        except asyncio.CancelledError:
            raise

    async def _close_current(self, reason: TerminalCloseReason) -> None:
        current = self._current
        if current is None:
            return
        self._current = None
        if current.size_task is not None:
            current.size_task.cancel()
        await current.remote.close()
        self._send(
            MessageType.TERMINAL_CLOSED,
            TerminalClosedPayload(
                terminal_id=current.remote.terminal_id,
                reason=reason,
            ),
        )

    async def _handle_open(self, request: TerminalOpenPayload) -> None:
        current = self._current
        if current is not None and request.terminal_id == current.remote.terminal_id:
            after_seq = request.after_seq
            exact_resume = (
                request.resume_stream_id == current.remote.stream_id
                and after_seq is not None
            )
            if exact_resume:
                assert after_seq is not None
                try:
                    replay = current.remote.replay_after(after_seq)
                except ReplayGap:
                    await self._close_current("stream_gap")
                else:
                    self._cancel_grace()
                    self._send_opened_and_bindings(current)
                    for chunk in replay:
                        await self._on_output(chunk)
                    return
            else:
                await self._close_current("stream_gap")
        elif current is not None:
            await self._close_current("replaced")
        elif request.resume_stream_id is not None:
            self._send(
                MessageType.TERMINAL_CLOSED,
                TerminalClosedPayload(
                    terminal_id=request.terminal_id,
                    reason="stream_gap",
                ),
            )
        self._cancel_grace()
        await self._open_fresh(request.terminal_id)

    async def _handle_action(self, action: TerminalActionPayload) -> None:
        current = self._current
        if current is None or current.remote.terminal_id != action.terminal_id:
            result = TerminalActionResultPayload(
                terminal_id=action.terminal_id,
                action_id=action.action_id,
                ok=False,
                error_code="stale_terminal",
            )
            self._send(MessageType.TERMINAL_ACTION_RESULT, result)
            return
        try:
            self._action_executor.execute(action)
        except ActionRejected as exc:
            result = TerminalActionResultPayload(
                terminal_id=action.terminal_id,
                action_id=action.action_id,
                ok=False,
                error_code=exc.code,
            )
        except Exception:
            result = TerminalActionResultPayload(
                terminal_id=action.terminal_id,
                action_id=action.action_id,
                ok=False,
                error_code="action_failed",
            )
        else:
            result = TerminalActionResultPayload(
                terminal_id=action.terminal_id,
                action_id=action.action_id,
                ok=True,
            )
        self._send(MessageType.TERMINAL_ACTION_RESULT, result)
        if result.ok:
            self._send_topology()

    async def _handle_rename(self, request: TermRenamePayload) -> None:
        if self._renamer is None:
            result = TermRenameResultPayload(
                command_id=request.command_id,
                ok=False,
                error_code="rename_unavailable",
            )
            self._send(MessageType.TERM_RENAME_RESULT, result)
            return
        try:
            topology = self._renamer.rename(request.name)
        except Exception:
            result = TermRenameResultPayload(
                command_id=request.command_id,
                ok=False,
                error_code="rename_failed",
            )
            self._send(MessageType.TERM_RENAME_RESULT, result)
            return
        self._send(
            MessageType.TERM_RENAME_RESULT,
            TermRenameResultPayload(command_id=request.command_id, ok=True),
        )
        self._send_topology(topology)

    def _invalid_message(self, message: WireMessage) -> None:
        raw_terminal_id = message.payload.get("terminal_id")
        try:
            terminal_id = UUID(str(raw_terminal_id))
        except (ValueError, TypeError):
            return
        self._send(
            MessageType.TERMINAL_CLOSED,
            TerminalClosedPayload(
                terminal_id=terminal_id,
                reason="internal_error",
                error_code="invalid_terminal_message",
            ),
        )

    async def handle_wire_message(self, message: WireMessage) -> None:
        try:
            payload = parse_payload(message.type, message.payload)
        except (ValidationError, ValueError):
            self._invalid_message(message)
            return
        async with self._lock:
            if message.type is MessageType.TERMINAL_OPEN:
                await self._handle_open(cast(TerminalOpenPayload, payload))
            elif message.type is MessageType.TERMINAL_INPUT:
                terminal_input = cast(TerminalInputPayload, payload)
                current = self._current
                if current is None or current.remote.terminal_id != terminal_input.terminal_id:
                    self._invalid_message(message)
                else:
                    await current.remote.write(terminal_input.to_bytes())
            elif message.type is MessageType.TERMINAL_ACTION:
                await self._handle_action(cast(TerminalActionPayload, payload))
            elif message.type is MessageType.TERMINAL_CLOSE:
                close = cast(TerminalClosePayload, payload)
                if self.current_terminal_id == close.terminal_id:
                    await self._close_current(close.reason)
                else:
                    self._invalid_message(message)
            elif message.type is MessageType.TERM_RENAME:
                await self._handle_rename(cast(TermRenamePayload, payload))
            else:
                self._invalid_message(message)

    async def close(self) -> None:
        self._cancel_grace()
        async with self._lock:
            if self._current is not None:
                current = self._current
                self._current = None
                if current.size_task is not None:
                    current.size_task.cancel()
                await current.remote.close()
