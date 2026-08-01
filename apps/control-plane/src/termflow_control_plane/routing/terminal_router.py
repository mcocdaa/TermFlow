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
    TerminalActionResultPayload,
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
    def record_nowait(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> object: ...

    async def flush(self) -> None: ...


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
        capability_wait_seconds: float = 5.0,
    ) -> None:
        self._registry = registry
        self._hub = hub
        self._audit = audit
        self._capability_wait_seconds = capability_wait_seconds

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

    def _record(
        self,
        operation: str,
        terminal: BrowserTerminal,
        *,
        pane_id: str | None = None,
        input_bytes: int | None = None,
        result: str = "ok",
        error_code: str | None = None,
    ) -> None:
        self._audit.record_nowait(
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

    def _record_open_rejection(self, instance_id: UUID, error_code: str) -> None:
        self._audit.record_nowait(
            "terminal.open",
            instance_id,
            None,
            None,
            "rejected",
            error_code,
        )

    async def open(
        self,
        instance_id: UUID,
        *,
        session_key: str | None,
    ) -> BrowserTerminal:
        try:
            connection = await self._registry.get(instance_id)
        except InstanceOffline as exc:
            self._record_open_rejection(instance_id, "instance_offline")
            raise TerminalRouteError("instance_offline") from exc
        try:
            async with asyncio.timeout(self._capability_wait_seconds):
                await connection.hello_ready.wait()
        except TimeoutError as exc:
            self._record_open_rejection(instance_id, "capability_unavailable")
            raise TerminalRouteError("capability_unavailable") from exc
        if "full_terminal" not in connection.capabilities:
            self._record_open_rejection(instance_id, "capability_unavailable")
            raise TerminalRouteError("capability_unavailable")
        terminal = await self._hub.register(instance_id, session_key=session_key)
        request = TerminalOpenPayload(terminal_id=terminal.terminal_id)
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_OPEN, request),
            )
        except TerminalRouteError as exc:
            await self._hub.unregister(terminal)
            terminal.terminate("instance_offline", error_code=exc.code)
            terminal.open_audited = True
            self._record(
                "terminal.open",
                terminal,
                result="rejected",
                error_code=exc.code,
            )
            raise
        return terminal

    async def bridge_connected(self, instance_id: UUID) -> None:
        try:
            connection = await self._registry.get(instance_id)
        except InstanceOffline:
            return
        if "full_terminal" not in connection.capabilities:
            return
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
        terminal = await self._hub.current(message.instance_id)
        forwarded = await self._hub.forward(message)
        if terminal is None or str(message.payload.get("terminal_id")) != str(
            terminal.terminal_id
        ):
            return forwarded
        if (
            message.type is MessageType.TERMINAL_OPENED
            and forwarded
            and not terminal.open_audited
        ):
            terminal.open_audited = True
            self._record("terminal.open", terminal)
        elif message.type is MessageType.TERMINAL_ACTION_RESULT:
            result = TerminalActionResultPayload.model_validate(message.payload)
            if result.action_id in terminal.pending_actions:
                pane_id = terminal.pending_actions.pop(result.action_id)
                self._record(
                    "terminal.action",
                    terminal,
                    pane_id=pane_id,
                    result="ok" if result.ok else "failed",
                    error_code=result.error_code,
                )
        return forwarded

    async def input(self, terminal: BrowserTerminal, data: bytes) -> None:
        payload = TerminalInputPayload.from_bytes(terminal.terminal_id, data)
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_INPUT, payload),
            )
        except TerminalRouteError as exc:
            self._record(
                "terminal.input",
                terminal,
                input_bytes=len(data),
                result="rejected",
                error_code=exc.code,
            )
            raise
        terminal.input_bytes += len(data)

    def _record_input_total(self, terminal: BrowserTerminal) -> None:
        if terminal.input_audited or terminal.input_bytes == 0:
            return
        terminal.input_audited = True
        self._record(
            "terminal.input",
            terminal,
            input_bytes=terminal.input_bytes,
        )

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
        terminal.pending_actions[frame.action_id] = frame.target_pane_id
        try:
            await self._enqueue(
                terminal,
                self._message(terminal, MessageType.TERMINAL_ACTION, payload),
            )
        except TerminalRouteError as exc:
            terminal.pending_actions.pop(frame.action_id, None)
            self._record(
                "terminal.action",
                terminal,
                pane_id=frame.target_pane_id,
                result="rejected",
                error_code=exc.code,
            )
            raise

    def _record_unresolved(self, terminal: BrowserTerminal) -> None:
        if not terminal.open_audited:
            terminal.open_audited = True
            self._record(
                "terminal.open",
                terminal,
                result="unknown",
                error_code="outcome_unknown",
            )
        for action_id, pane_id in tuple(terminal.pending_actions.items()):
            terminal.pending_actions.pop(action_id, None)
            self._record(
                "terminal.action",
                terminal,
                pane_id=pane_id,
                result="unknown",
                error_code="outcome_unknown",
            )

    async def request_close(
        self,
        terminal: BrowserTerminal,
        reason: TerminalCloseReason,
    ) -> None:
        if terminal.close_requested:
            return
        terminal.close_requested = True
        error_code: str | None = None
        self._record_input_total(terminal)
        self._record_unresolved(terminal)
        # Persist accumulated metadata before the teardown frame becomes observable.
        await self._audit.flush()
        current = await self._hub.current(terminal.instance_id)
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
                self._registry.force_disconnect_current_nowait(terminal.instance_id)
        await self._hub.unregister(terminal)
        terminal.close_audited = True
        self._record(
            "terminal.close",
            terminal,
            result="unknown" if error_code is not None else "ok",
            error_code=error_code,
        )
        await self._audit.flush()

    def abandon(self, terminal: BrowserTerminal) -> None:
        """Detach on ASGI cancellation without starting database cleanup work."""

        terminal.close_requested = True
        self._record_input_total(terminal)
        self._record_unresolved(terminal)
        was_current = self._hub.abandon(terminal)
        close_sent = terminal.remote_closed or not was_current
        if was_current and not terminal.remote_closed:
            payload = TerminalClosePayload(
                terminal_id=terminal.terminal_id,
                reason="client_closed",
            )
            close_sent = self._registry.enqueue_current_nowait(
                terminal.instance_id,
                self._message(terminal, MessageType.TERMINAL_CLOSE, payload),
            )
            if not close_sent:
                self._registry.force_disconnect_current_nowait(terminal.instance_id)
        if not terminal.close_audited:
            terminal.close_audited = True
            self._record(
                "terminal.close",
                terminal,
                result="ok" if close_sent else "unknown",
                error_code=None if close_sent else "backpressure",
            )
