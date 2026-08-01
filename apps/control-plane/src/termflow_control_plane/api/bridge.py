"""Authenticated WebSocket carried by each local TermFlow Bridge."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    BridgeHeartbeatPayload,
    BridgeHelloPayload,
    CommandResultPayload,
    InstancePresencePayload,
    MessageType,
    TerminalActionResultPayload,
    TerminalBindingsPayload,
    TerminalClosedPayload,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    TerminalSizePayload,
    TermRenameResultPayload,
    TopologyChangedPayload,
    TopologySnapshotPayload,
    WireMessage,
    parse_payload,
)

from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.terminal_router import TerminalRouter

router = APIRouter(prefix="/api/v1/bridge", tags=["bridge"])


def _bearer_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


async def _send_messages(websocket: WebSocket, connection: LiveConnection) -> None:
    while True:
        message = await connection.outbound.get()
        await websocket.send_text(message.model_dump_json())


async def _receive_messages(
    websocket: WebSocket,
    connection: LiveConnection,
    event_hub: EventHub,
    repositories: RepositoryBundle,
    registry: LiveInstanceRegistry,
    terminal_router: TerminalRouter,
) -> None:
    while True:
        message = WireMessage.model_validate_json(await websocket.receive_text())
        if message.instance_id != connection.instance_id:
            await websocket.close(code=4403, reason="Instance identity mismatch")
            return
        if await registry.maybe_get(connection.instance_id) is not connection:
            return

        payload = parse_payload(message.type, message.payload)
        if message.type is MessageType.BRIDGE_HELLO:
            cast(BridgeHelloPayload, payload)
            connection.last_heartbeat = datetime.now(UTC)
            await repositories.instances.touch(
                connection.instance_id,
                now=connection.last_heartbeat,
            )
        elif message.type is MessageType.BRIDGE_HEARTBEAT:
            cast(BridgeHeartbeatPayload, payload)
            connection.last_heartbeat = datetime.now(UTC)
            await repositories.instances.touch(
                connection.instance_id,
                now=connection.last_heartbeat,
            )
        elif message.type in {MessageType.TOPOLOGY_SNAPSHOT, MessageType.TOPOLOGY_CHANGED}:
            topology_payload = cast(
                TopologySnapshotPayload | TopologyChangedPayload,
                payload,
            )
            await repositories.instances.update_from_topology(
                connection.instance_id,
                topology_payload.topology.session_name,
            )
            connection.topology = topology_payload.topology
            connection.topology_ready.set()
        elif message.type is MessageType.COMMAND_RESULT:
            result = cast(CommandResultPayload, payload)
            future = connection.pending.pop(result.command_id, None)
            if future is not None and not future.done():
                future.set_result(result)
        elif message.type is MessageType.TERM_RENAME_RESULT:
            rename_result = cast(TermRenameResultPayload, payload)
            rename_future = connection.pending_renames.pop(rename_result.command_id, None)
            if rename_future is not None and not rename_future.done():
                rename_future.set_result(rename_result)
        elif message.type in {
            MessageType.TERMINAL_OPENED,
            MessageType.TERMINAL_OUTPUT,
            MessageType.TERMINAL_SIZE,
            MessageType.TERMINAL_BINDINGS,
            MessageType.TERMINAL_ACTION_RESULT,
            MessageType.TERMINAL_CLOSED,
        }:
            cast(
                TerminalOpenedPayload
                | TerminalOutputPayload
                | TerminalSizePayload
                | TerminalBindingsPayload
                | TerminalActionResultPayload
                | TerminalClosedPayload,
                payload,
            )
            await terminal_router.forward_from_bridge(message)
        elif message.type in {MessageType.PANE_OUTPUT, MessageType.STREAM_GAP}:
            await event_hub.publish(message)

        if message.type in {
            MessageType.TOPOLOGY_SNAPSHOT,
            MessageType.TOPOLOGY_CHANGED,
        }:
            await event_hub.publish(message)


def _presence_message(
    connection: LiveConnection,
    status: Literal["online", "offline"],
) -> WireMessage:
    payload = InstancePresencePayload(status=status)
    message_type = (
        MessageType.INSTANCE_ONLINE if status == "online" else MessageType.INSTANCE_OFFLINE
    )
    return WireMessage(
        type=message_type,
        instance_id=connection.instance_id,
        payload=payload.model_dump(mode="json"),
    )


async def _close_when_replaced(websocket: WebSocket, connection: LiveConnection) -> None:
    await connection.replaced.wait()
    await websocket.close(code=4409, reason="Connection replaced")


@router.websocket("/connect")
async def connect_bridge(websocket: WebSocket) -> None:
    repositories = cast(RepositoryBundle, websocket.app.state.repositories)
    registry = cast(LiveInstanceRegistry, websocket.app.state.registry)
    event_hub = cast(EventHub, websocket.app.state.event_hub)
    terminal_router = cast(TerminalRouter, websocket.app.state.terminal_router)
    token = _bearer_token(websocket)
    instance = (
        await repositories.instances.get_by_token_hash(hash_token(token))
        if token is not None
        else None
    )
    if instance is None:
        await websocket.close(code=4401, reason="Authentication required")
        return

    await websocket.accept()
    connection = await registry.register(instance.id)
    await terminal_router.bridge_connected(instance.id)
    await event_hub.publish(_presence_message(connection, "online"))
    tasks = {
        asyncio.create_task(_send_messages(websocket, connection)),
        asyncio.create_task(
            _receive_messages(
                websocket,
                connection,
                event_hub,
                repositories,
                registry,
                terminal_router,
            )
        ),
        asyncio.create_task(_close_when_replaced(websocket, connection)),
    }
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
        for task in pending:
            with suppress(WebSocketDisconnect, asyncio.CancelledError):
                await task
    except (ValidationError, ValueError):
        with suppress(RuntimeError):
            await websocket.close(code=4400, reason="Invalid protocol message")
    finally:
        for task in tasks:
            task.cancel()
        if await registry.unregister(connection):
            await event_hub.publish(_presence_message(connection, "offline"))
