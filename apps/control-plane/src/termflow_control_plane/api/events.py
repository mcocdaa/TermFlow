"""Read-only WebSocket subscriptions for ephemeral Instance events."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import cast
from uuid import UUID

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import MessageType, PaneReplayRequestPayload, WireMessage

from termflow_control_plane.auth.dpop import DpopVerifier
from termflow_control_plane.auth.epoch import persisted_authentication_epoch
from termflow_control_plane.auth.sessions import (
    BrowserSessionStore,
    authenticate_admin_websocket,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub, EventSubscriber
from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveInstanceRegistry,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle

router = APIRouter(tags=["events"])


async def _send_subscription(
    websocket: WebSocket,
    subscriber: EventSubscriber,
    repositories: RepositoryBundle,
    auth_epoch: int,
) -> None:
    disconnected = asyncio.create_task(websocket.receive())
    try:
        while True:
            next_event = asyncio.create_task(subscriber.queue.get())
            closed = asyncio.create_task(subscriber.closed.wait())
            try:
                done, _ = await asyncio.wait(
                    {next_event, closed, disconnected},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (next_event, closed):
                    if not task.done():
                        task.cancel()
                for task in (next_event, closed):
                    if not task.done():
                        with suppress(asyncio.CancelledError):
                            await task
            if disconnected in done:
                with suppress(WebSocketDisconnect):
                    disconnected.result()
                return
            if closed in done:
                await websocket.close(
                    code=subscriber.close_code,
                    reason=subscriber.close_reason,
                )
                return
            if await persisted_authentication_epoch(repositories) != auth_epoch:
                await websocket.close(code=4401, reason="Authentication epoch changed")
                return
            await websocket.send_text(next_event.result().model_dump_json())
    finally:
        if not disconnected.done():
            disconnected.cancel()
        with suppress(WebSocketDisconnect, asyncio.CancelledError):
            await disconnected


@router.websocket("/api/v1/events")
async def subscribe_events(
    websocket: WebSocket,
    instance_id: UUID,
    pane_id: str | None = None,
    stream_id: UUID | None = None,
    after_seq: int | None = None,
) -> None:
    settings = cast(Settings, websocket.app.state.settings)
    sessions = cast(BrowserSessionStore, websocket.app.state.browser_sessions)
    repositories = cast(RepositoryBundle, websocket.app.state.repositories)
    registry = cast(LiveInstanceRegistry, websocket.app.state.registry)
    hub = cast(EventHub, websocket.app.state.event_hub)
    dpop = cast(DpopVerifier, websocket.app.state.dpop_verifier)
    authentication = await authenticate_admin_websocket(
        websocket,
        settings,
        sessions,
        repositories,
        dpop,
        required_scope="terminal.read",
    )
    if authentication.close_code is not None:
        reason = (
            "Origin not allowed"
            if authentication.close_code == 4403
            else "Authentication required"
        )
        await websocket.close(code=authentication.close_code, reason=reason)
        return
    assert authentication.epoch is not None
    auth_epoch = authentication.epoch
    if await repositories.instances.get(instance_id) is None:
        await websocket.close(code=4404, reason="Instance not found")
        return

    cursor = (pane_id, stream_id, after_seq)
    if any(value is not None for value in cursor) and not all(
        value is not None for value in cursor
    ):
        await websocket.close(code=4400, reason="Replay cursor is incomplete")
        return
    if after_seq is not None and after_seq < 0:
        await websocket.close(code=4400, reason="Replay cursor is invalid")
        return
    if (await repositories.auth_state.get()).epoch != auth_epoch:
        await websocket.close(code=4401, reason="Authentication epoch changed")
        return

    subscriber = await hub.subscribe(instance_id, auth_epoch=auth_epoch)
    if subscriber.closed.is_set():
        await websocket.close(
            code=subscriber.close_code,
            reason=subscriber.close_reason,
        )
        return
    await websocket.accept()
    try:
        if pane_id is not None and stream_id is not None and after_seq is not None:
            payload = PaneReplayRequestPayload(
                pane_id=pane_id,
                stream_id=stream_id,
                after_seq=after_seq,
            )
            try:
                await registry.enqueue(
                    instance_id,
                    WireMessage(
                        type=MessageType.PANE_REPLAY_REQUEST,
                        instance_id=instance_id,
                        payload=payload.model_dump(mode="json"),
                    ),
                )
            except InstanceOffline:
                await websocket.close(code=4409, reason="Instance offline")
                return
            except ConnectionBackpressure:
                await websocket.close(code=4429, reason="Bridge queue full")
                return
        await _send_subscription(websocket, subscriber, repositories, auth_epoch)
    finally:
        await hub.unsubscribe(subscriber)
