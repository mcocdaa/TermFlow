"""Ephemeral terminal routing between one browser owner and one live Bridge."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from termflow_protocol import (
    MessageType,
    TerminalActionFrame,
    TerminalActionPayload,
    TerminalClosePayload,
    TerminalCloseReason,
    TerminalInputPayload,
    TerminalOpenPayload,
    WireMessage,
)

from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveInstanceRegistry,
)
from termflow_control_plane.connections.terminal_hub import BrowserTerminal, TerminalHub


class AuditWriter(Protocol):
    async def record(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> object: ...


class TerminalRouteError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TerminalRouter:
    def __init__(
        self,
        *,
        registry: LiveInstanceRegistry,
        hub: TerminalHub,
        audit: AuditWriter,
        resume_grace_seconds: float = 30.0,
    ) -> None:
        self._registry = registry
        self._hub = hub
        self._audit = audit
        self._resume_grace_seconds = resume_grace_seconds
        self._suspended: dict[UUID, asyncio.Task[None]] = {}

    def _cancel_suspension(self, terminal_id: UUID) -> None:
        task = self._suspended.pop(terminal_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @staticmethod
    def _message(
        terminal: BrowserTerminal,
        message_type: MessageType,
        payload: BaseModel,
    ) -> WireMessage:
        return WireMessage(
            type=message_type,
            instance_id=terminal.instance_id,
            payload=payload.model_dump(mode="json"),
        )

    async def _record(
        self,
        operation: str,
        terminal: BrowserTerminal,
        *,
        pane_id: str | None = None,
        input_bytes: int | None = None,
        result: str = "ok",
        error_code: str | None = None,
    ) -> None:
        await self._audit.record(
            operation,
            terminal.instance_id,
            pane_id,
            input_bytes,
            result,
            error_code,
        )

    async def _enqueue(self, terminal: BrowserTerminal, message: WireMessage) -> None:
        try:
            await self._registry.enqueue(terminal.instance_id, message)
        except InstanceOffline as exc:
            raise TerminalRouteError("instance_offline") from exc
        except ConnectionBackpressure as exc:
            raise TerminalRouteError("backpressure") from exc

    async def open(
        self,
        instance_id: UUID,
        *,
        session_key: str | None,
    ) -> BrowserTerminal:
        try:
            await self._registry.get(instance_id)
        except InstanceOffline as exc:
            raise TerminalRouteError("instance_offline") from exc
        terminal = await self._hub.register(instance_id, session_key=session_key)
        request = TerminalOpenPayload(terminal_id=terminal.terminal_id)
        await self._record("terminal.open", terminal)
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_OPEN, request),
            )
        except TerminalRouteError as exc:
            await self._hub.unregister(terminal)
            terminal.terminate("instance_offline", error_code=exc.code)
            await self._record(
                "terminal.open",
                terminal,
                result="rejected",
                error_code=exc.code,
            )
            raise
        return terminal

    async def resume(
        self,
        instance_id: UUID,
        *,
        session_key: str | None,
        terminal_id: UUID,
        stream_id: UUID,
        after_seq: int,
    ) -> BrowserTerminal | None:
        terminal = await self._hub.resume(
            instance_id,
            session_key=session_key,
            terminal_id=terminal_id,
            stream_id=stream_id,
            after_seq=after_seq,
        )
        if terminal is None:
            return None
        self._cancel_suspension(terminal.terminal_id)
        request = TerminalOpenPayload(
            terminal_id=terminal.terminal_id,
            resume_stream_id=stream_id,
            after_seq=after_seq,
        )
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_OPEN, request),
            )
        except TerminalRouteError:
            terminal.terminate("instance_offline", error_code="reconnect_failed")
            return None
        return terminal

    async def bridge_connected(self, instance_id: UUID) -> None:
        terminal = await self._hub.current(instance_id)
        if terminal is None or terminal.terminated or terminal.remote_closed:
            return
        cursor = terminal.resume_cursor
        if cursor is None:
            request = TerminalOpenPayload(terminal_id=terminal.terminal_id)
        else:
            stream_id, last_seq = cursor
            request = TerminalOpenPayload(
                terminal_id=terminal.terminal_id,
                resume_stream_id=stream_id,
                after_seq=last_seq,
            )
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_OPEN, request),
            )
        except TerminalRouteError:
            terminal.terminate("instance_offline", error_code="reconnect_failed")

    async def forward_from_bridge(self, message: WireMessage) -> bool:
        return await self._hub.forward(message)

    async def input(self, terminal: BrowserTerminal, data: bytes) -> None:
        payload = TerminalInputPayload.from_bytes(terminal.terminal_id, data)
        await self._record(
            "terminal.input",
            terminal,
            input_bytes=len(data),
        )
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_INPUT, payload),
            )
        except TerminalRouteError as exc:
            await self._record(
                "terminal.input",
                terminal,
                input_bytes=len(data),
                result="rejected",
                error_code=exc.code,
            )
            raise

    async def action(
        self,
        terminal: BrowserTerminal,
        frame: TerminalActionFrame,
    ) -> None:
        payload = TerminalActionPayload(
            terminal_id=terminal.terminal_id,
            action_id=frame.action_id,
            action=frame.action,
            target_pane_id=frame.target_pane_id,
            confirmed=frame.confirmed,
        )
        await self._record(
            "terminal.action",
            terminal,
            pane_id=frame.target_pane_id,
        )
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_ACTION, payload),
            )
        except TerminalRouteError as exc:
            await self._record(
                "terminal.action",
                terminal,
                pane_id=frame.target_pane_id,
                result="rejected",
                error_code=exc.code,
            )
            raise

    async def request_close(
        self,
        terminal: BrowserTerminal,
        reason: TerminalCloseReason,
    ) -> None:
        self._cancel_suspension(terminal.terminal_id)
        if terminal.close_requested:
            return
        terminal.close_requested = True
        current = await self._hub.current(terminal.instance_id)
        error_code: str | None = None
        await self._record(
            "terminal.close",
            terminal,
            result="ok",
        )
        if current is terminal and not terminal.remote_closed:
            payload = TerminalClosePayload(
                terminal_id=terminal.terminal_id,
                reason=reason,
            )
            try:
                await self._enqueue(
                    terminal,
                    self._message(terminal, MessageType.TERMINAL_CLOSE, payload),
                )
            except TerminalRouteError as exc:
                error_code = exc.code
        await self._hub.unregister(terminal)
        if error_code is not None:
            await self._record(
                "terminal.close",
                terminal,
                result="unknown",
                error_code=error_code,
            )

    def suspend(self, terminal: BrowserTerminal) -> None:
        """Retain a disconnected browser cursor briefly without persisting bytes."""

        if terminal.close_requested or terminal.remote_closed or terminal.terminated:
            self._hub.abandon(terminal)
            return
        if terminal.terminal_id in self._suspended:
            return

        async def expire() -> None:
            try:
                await asyncio.sleep(self._resume_grace_seconds)
                self._suspended.pop(terminal.terminal_id, None)
                await self.request_close(terminal, "grace_expired")
                terminal.terminate("grace_expired")
            except asyncio.CancelledError:
                raise

        self._suspended[terminal.terminal_id] = asyncio.create_task(expire())

    def abandon(self, terminal: BrowserTerminal) -> None:
        """Detach on ASGI cancellation without starting database cleanup work."""

        if terminal.close_requested or terminal.remote_closed:
            self._cancel_suspension(terminal.terminal_id)
            self._hub.abandon(terminal)
            return
        self._cancel_suspension(terminal.terminal_id)
        terminal.close_requested = True
        payload = TerminalClosePayload(
            terminal_id=terminal.terminal_id,
            reason="client_closed",
        )
        self._registry.enqueue_current_nowait(
            terminal.instance_id,
            self._message(terminal, MessageType.TERMINAL_CLOSE, payload),
        )
        self._hub.abandon(terminal)
