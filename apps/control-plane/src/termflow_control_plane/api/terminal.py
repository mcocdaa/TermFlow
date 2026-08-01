"""Authenticated browser/native WebSocket adapter for full tmux terminals."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    MessageType,
    TerminalActionFrame,
    TerminalActionResultFrame,
    TerminalActionResultPayload,
    TerminalBindingSnapshotFrame,
    TerminalBindingsPayload,
    TerminalClosedFrame,
    TerminalClosedPayload,
    TerminalCloseFrame,
    TerminalErrorFrame,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    TerminalReadyFrame,
    TerminalSizeFrame,
    TerminalSizePayload,
)

from termflow_control_plane.auth.sessions import (
    BrowserSessionStore,
    websocket_admin_close_code,
    websocket_browser_session_key,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.terminal_hub import (
    BrowserTerminal,
    LocalTerminalClose,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.terminal_router import (
    TerminalRouteError,
    TerminalRouter,
)

router = APIRouter(prefix="/api/v1/terms", tags=["terminal"])

_ERROR_MESSAGES = {
    "backpressure": "The terminal connection could not keep up.",
    "capability_unavailable": "The Term does not support full terminal access.",
    "frame_too_large": "The terminal frame exceeds the configured limit.",
    "input_rate_exceeded": "The terminal input rate limit was exceeded.",
    "instance_offline": "The Term is offline.",
    "invalid_control_frame": "The terminal control frame is invalid.",
    "stream_gap": "The retained terminal stream cannot be resumed.",
    "target_not_found": "The selected tmux target no longer exists.",
}


@dataclass(slots=True)
class _TokenBucket:
    rate: int
    tokens: float
    observed_at: float

    @classmethod
    def create(cls, rate: int) -> _TokenBucket:
        return cls(rate=rate, tokens=float(rate), observed_at=time.monotonic())

    def consume(self, byte_count: int) -> bool:
        now = time.monotonic()
        self.tokens = min(
            float(self.rate),
            self.tokens + (now - self.observed_at) * self.rate,
        )
        self.observed_at = now
        if byte_count > self.tokens:
            return False
        self.tokens -= byte_count
        return True


def _error_frame(terminal_id: UUID, code: str) -> TerminalErrorFrame:
    return TerminalErrorFrame(
        terminal_id=terminal_id,
        code=code,
        message=_ERROR_MESSAGES.get(code, "The terminal operation failed."),
    )


async def _send_text_model(websocket: WebSocket, model: BaseModel) -> None:
    await websocket.send_text(model.model_dump_json())


async def _send_terminal_events(
    websocket: WebSocket,
    terminal: BrowserTerminal,
    terminal_router: TerminalRouter,
) -> None:
    while True:
        event = await terminal.next_event()
        if isinstance(event, LocalTerminalClose):
            await terminal_router.request_close(terminal, event.reason)
            if event.error_code is not None:
                await _send_text_model(
                    websocket,
                    _error_frame(terminal.terminal_id, event.error_code),
                )
            await _send_text_model(
                websocket,
                TerminalClosedFrame(
                    terminal_id=terminal.terminal_id,
                    reason=event.reason,
                ),
            )
            with suppress(RuntimeError):
                await websocket.close(code=1000)
            return

        if event.type is MessageType.TERMINAL_OPENED:
            opened = TerminalOpenedPayload.model_validate(event.payload)
            await _send_text_model(
                websocket,
                TerminalReadyFrame(
                    terminal_id=opened.terminal_id,
                    stream_id=opened.stream_id,
                    rows=opened.rows,
                    cols=opened.cols,
                ),
            )
        elif event.type is MessageType.TERMINAL_OUTPUT:
            output = TerminalOutputPayload.model_validate(event.payload)
            await websocket.send_bytes(output.to_bytes())
        elif event.type is MessageType.TERMINAL_SIZE:
            size = TerminalSizePayload.model_validate(event.payload)
            await _send_text_model(
                websocket,
                TerminalSizeFrame(
                    terminal_id=size.terminal_id,
                    rows=size.rows,
                    cols=size.cols,
                ),
            )
        elif event.type is MessageType.TERMINAL_BINDINGS:
            bindings = TerminalBindingsPayload.model_validate(event.payload)
            await _send_text_model(
                websocket,
                TerminalBindingSnapshotFrame(
                    terminal_id=bindings.terminal_id,
                    prefix=bindings.prefix,
                    prefix2=bindings.prefix2,
                    bindings=bindings.bindings,
                ),
            )
        elif event.type is MessageType.TERMINAL_ACTION_RESULT:
            result = TerminalActionResultPayload.model_validate(event.payload)
            await _send_text_model(
                websocket,
                TerminalActionResultFrame(
                    terminal_id=result.terminal_id,
                    action_id=result.action_id,
                    ok=result.ok,
                    error_code=result.error_code,
                ),
            )
            if not result.ok:
                await _send_text_model(
                    websocket,
                    _error_frame(
                        result.terminal_id,
                        result.error_code or "action_failed",
                    ),
                )
        elif event.type is MessageType.TERMINAL_CLOSED:
            closed = TerminalClosedPayload.model_validate(event.payload)
            await terminal_router.request_close(terminal, closed.reason)
            if closed.error_code is not None:
                await _send_text_model(
                    websocket,
                    _error_frame(closed.terminal_id, closed.error_code),
                )
            await _send_text_model(
                websocket,
                TerminalClosedFrame(
                    terminal_id=closed.terminal_id,
                    reason=closed.reason,
                ),
            )
            with suppress(RuntimeError):
                await websocket.close(code=1000)
            return


async def _receive_terminal_input(
    websocket: WebSocket,
    terminal: BrowserTerminal,
    terminal_router: TerminalRouter,
    settings: Settings,
) -> None:
    bucket = _TokenBucket.create(settings.terminal_input_rate_bytes_per_second)
    while True:
        incoming = await websocket.receive()
        if incoming["type"] == "websocket.disconnect":
            return
        data = incoming.get("bytes")
        if data is not None:
            if len(data) > settings.terminal_max_frame_bytes:
                terminal.terminate("internal_error", error_code="frame_too_large")
                return
            if not bucket.consume(len(data)):
                terminal.terminate("internal_error", error_code="input_rate_exceeded")
                return
            try:
                await terminal_router.input(terminal, data)
            except TerminalRouteError as exc:
                terminal.terminate("internal_error", error_code=exc.code)
                return
            continue

        text = incoming.get("text")
        try:
            raw = json.loads(text) if isinstance(text, str) else None
            if not isinstance(raw, dict):
                raise ValueError
            frame_type = raw.get("type")
            if frame_type == "terminal.action":
                action = TerminalActionFrame.model_validate(raw)
                await terminal_router.action(terminal, action)
            elif frame_type == "terminal.close":
                close = TerminalCloseFrame.model_validate(raw)
                await terminal_router.request_close(terminal, close.reason)
                terminal.terminate(close.reason)
                return
            else:
                raise ValueError
        except (TerminalRouteError, ValidationError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, TerminalRouteError) else "invalid_control_frame"
            terminal.terminate("internal_error", error_code=code)
            return


@router.websocket("/{instance_id}/terminal")
async def connect_terminal(websocket: WebSocket, instance_id: UUID) -> None:
    settings = cast(Settings, websocket.app.state.settings)
    sessions = cast(BrowserSessionStore, websocket.app.state.browser_sessions)
    repositories = cast(RepositoryBundle, websocket.app.state.repositories)
    terminal_router = cast(TerminalRouter, websocket.app.state.terminal_router)
    auth_close_code = websocket_admin_close_code(websocket, settings, sessions)
    if auth_close_code is not None:
        await websocket.close(code=auth_close_code, reason="Authentication or Origin rejected")
        return
    if await repositories.instances.get(instance_id) is None:
        await websocket.close(code=4404, reason="Term not found")
        return

    session_key = websocket_browser_session_key(websocket, settings, sessions)
    await websocket.accept()
    raw_terminal_id = websocket.query_params.get("terminal_id")
    raw_stream_id = websocket.query_params.get("stream_id")
    raw_after_seq = websocket.query_params.get("after_seq")
    resume_values = (raw_terminal_id, raw_stream_id, raw_after_seq)
    terminal: BrowserTerminal | None = None
    if any(value is not None for value in resume_values):
        try:
            if not all(value is not None for value in resume_values):
                raise ValueError
            terminal = await terminal_router.resume(
                instance_id,
                session_key=session_key,
                terminal_id=UUID(str(raw_terminal_id)),
                stream_id=UUID(str(raw_stream_id)),
                after_seq=int(str(raw_after_seq)),
            )
        except (TypeError, ValueError):
            await websocket.close(code=4400, reason="Invalid terminal resume cursor")
            return
    try:
        if terminal is None:
            terminal = await terminal_router.open(instance_id, session_key=session_key)
    except TerminalRouteError as exc:
        temporary_id = UUID(int=0)
        await _send_text_model(websocket, _error_frame(temporary_id, exc.code))
        await websocket.close(code=4409)
        return

    sender = asyncio.create_task(
        _send_terminal_events(websocket, terminal, terminal_router)
    )
    receiver = asyncio.create_task(
        _receive_terminal_input(websocket, terminal, terminal_router, settings)
    )
    tasks = {sender, receiver}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if receiver in done and terminal.terminated and sender in pending:
            with suppress(WebSocketDisconnect, asyncio.CancelledError):
                await sender
            done.add(sender)
            pending.discard(sender)
        for task in pending:
            task.cancel()
        for task in done:
            with suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
        for task in pending:
            with suppress(WebSocketDisconnect, asyncio.CancelledError):
                await task
    finally:
        for task in tasks:
            task.cancel()
        terminal_router.suspend(terminal)
