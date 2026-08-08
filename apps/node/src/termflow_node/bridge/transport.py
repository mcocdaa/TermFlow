"""Registration and resilient WebSocket transport for one Bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from typing import Protocol

from termflow_protocol import (
    BridgeHeartbeatPayload,
    BridgeHelloPayload,
    MessageType,
    TopologySnapshot,
    TopologySnapshotPayload,
    WireMessage,
)
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from termflow_node.config.models import InstallationConfig
from termflow_node.control_plane_client import ControlPlaneClient, validate_server_url
from termflow_node.instances.models import LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore

from .backoff import ReconnectBackoff


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class ConnectWebSocket(Protocol):
    def __call__(
        self,
        uri: str,
        *,
        additional_headers: Mapping[str, str],
        ping_interval: float | None,
    ) -> AbstractAsyncContextManager[WebSocketLike]: ...


class ConnectionListener(Protocol):
    def bridge_connected(self) -> None: ...

    def bridge_disconnected(self) -> None: ...


def _connect_websocket(
    uri: str,
    *,
    additional_headers: Mapping[str, str],
    ping_interval: float | None,
) -> AbstractAsyncContextManager[ClientConnection]:
    return websocket_connect(
        uri,
        additional_headers=additional_headers,
        ping_interval=ping_interval,
    )


def bridge_websocket_url(server_url: str, *, allow_insecure_http: bool = False) -> str:
    base_url = validate_server_url(server_url, allow_insecure_http=allow_insecure_http)
    if base_url.startswith("https://"):
        return f"wss://{base_url.removeprefix('https://')}/api/v1/bridge/connect"
    if base_url.startswith("http://"):
        return f"ws://{base_url.removeprefix('http://')}/api/v1/bridge/connect"
    raise ValueError("unsupported Control Plane URL")


def is_instance_credential_rejection(error: BaseException) -> bool:
    """Classify only deterministic rejection of an issued Instance credential."""

    if isinstance(error, InvalidStatus):
        return error.response.status_code == 403
    if not isinstance(error, ConnectionClosed):
        return False
    received_code = error.rcvd.code if error.rcvd is not None else None
    sent_code = error.sent.code if error.sent is not None else None
    return received_code == 4401 or sent_code == 4401


class BridgeTransport:
    _CONTROL_RESERVE = 8
    _CONTROL_TYPES = {
        MessageType.BRIDGE_HEARTBEAT,
        MessageType.TOPOLOGY_CHANGED,
        MessageType.TERMINAL_OPENED,
        MessageType.TERMINAL_BINDINGS,
        MessageType.TERMINAL_SIZE,
        MessageType.TERMINAL_ACTION_RESULT,
        MessageType.TERM_RENAME_RESULT,
    }

    def __init__(
        self,
        *,
        installation: InstallationConfig,
        instance: LocalInstance,
        store: InstanceStore,
        control_plane: ControlPlaneClient,
        topology_provider: Callable[[], TopologySnapshot],
        connect: ConnectWebSocket = _connect_websocket,
        heartbeat_interval: float = 15.0,
        queue_size: int = 256,
        backoff: ReconnectBackoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._installation = installation
        self._instance = instance
        self._store = store
        self._control_plane = control_plane
        self._topology_provider = topology_provider
        self._connect = connect
        self._heartbeat_interval = heartbeat_interval
        self._normal_queue_size = queue_size
        self._outbound: asyncio.Queue[WireMessage] = asyncio.Queue(
            maxsize=queue_size + self._CONTROL_RESERVE + 1
        )
        self._backoff = backoff or ReconnectBackoff()
        self._sleep = sleep
        self._connection_listener: ConnectionListener | None = None

    @property
    def instance(self) -> LocalInstance:
        return self._instance

    def enqueue_nowait(self, message: WireMessage) -> bool:
        if message.type is MessageType.TERMINAL_CLOSED:
            self._discard_queued_terminal_closed()
        queued = self._outbound.qsize()
        if message.type is MessageType.TERMINAL_CLOSED:
            limit = self._normal_queue_size + self._CONTROL_RESERVE + 1
        elif message.type in self._CONTROL_TYPES:
            limit = self._normal_queue_size + self._CONTROL_RESERVE
        else:
            limit = self._normal_queue_size
        if queued >= limit:
            return False
        try:
            self._outbound.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    def _discard_queued_terminal_closed(self) -> None:
        """Keep only the latest one-owner terminal teardown under stalled I/O."""

        retained: list[WireMessage] = []
        while not self._outbound.empty():
            queued = self._outbound.get_nowait()
            if queued.type is not MessageType.TERMINAL_CLOSED:
                retained.append(queued)
        for queued in retained:
            self._outbound.put_nowait(queued)

    def set_connection_listener(self, listener: ConnectionListener) -> None:
        self._connection_listener = listener

    def _discard_terminal_outbound(self) -> None:
        retained: list[WireMessage] = []
        while not self._outbound.empty():
            message = self._outbound.get_nowait()
            if not message.type.value.startswith("terminal."):
                retained.append(message)
        for message in retained:
            self._outbound.put_nowait(message)

    def _mark_activation_required(self) -> None:
        self._instance = self._instance.model_copy(
            update={
                "schema_version": 4,
                "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
                "instance_token": None,
                "bridge_pid": None,
            }
        )
        self._store.save(self._instance)

    async def run(
        self,
        handler: Callable[[WireMessage], Awaitable[None]],
        shutdown: asyncio.Event,
    ) -> None:
        if self._instance.remote_access is RemoteAccessState.ACTIVATION_REQUIRED:
            return
        while not shutdown.is_set():
            try:
                if self._instance.instance_token is None:
                    self._instance = await self._control_plane.register_instance(
                        self._installation,
                        self._instance,
                        self._store,
                    )
                await self._run_connected(handler, shutdown)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if is_instance_credential_rejection(error):
                    self._mark_activation_required()
                    return
                if shutdown.is_set():
                    return
                await self._sleep(self._backoff.next_delay())

    async def _run_connected(
        self,
        handler: Callable[[WireMessage], Awaitable[None]],
        shutdown: asyncio.Event,
    ) -> None:
        token = self._instance.instance_token
        if token is None:
            raise RuntimeError("Instance registration did not produce a credential")
        async with self._connect(
            bridge_websocket_url(
                str(self._installation.server_url),
                allow_insecure_http=self._installation.allow_insecure_http,
            ),
            additional_headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
            },
            ping_interval=None,
        ) as websocket:
            if self._connection_listener is not None:
                self._connection_listener.bridge_connected()
            try:
                await websocket.send(
                    WireMessage(
                        type=MessageType.BRIDGE_HELLO,
                        instance_id=self._instance.instance_id,
                        payload=BridgeHelloPayload(name=self._instance.name).model_dump(
                            mode="json"
                        ),
                    ).model_dump_json()
                )
                topology = self._topology_provider()
                await websocket.send(
                    WireMessage(
                        type=MessageType.TOPOLOGY_SNAPSHOT,
                        instance_id=self._instance.instance_id,
                        payload=TopologySnapshotPayload(topology=topology).model_dump(mode="json"),
                    ).model_dump_json()
                )
                self._backoff.reset()
                tasks = {
                    asyncio.create_task(self._send_loop(websocket)),
                    asyncio.create_task(self._receive_loop(websocket, handler)),
                    asyncio.create_task(self._heartbeat_loop()),
                    asyncio.create_task(shutdown.wait()),
                }
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task
                for task in done:
                    task.result()
            finally:
                if self._connection_listener is not None:
                    self._connection_listener.bridge_disconnected()
                self._discard_terminal_outbound()

    async def _send_loop(self, websocket: WebSocketLike) -> None:
        while True:
            message = await self._outbound.get()
            await websocket.send(message.model_dump_json())

    async def _receive_loop(
        self,
        websocket: WebSocketLike,
        handler: Callable[[WireMessage], Awaitable[None]],
    ) -> None:
        while True:
            message = WireMessage.model_validate_json(await websocket.recv())
            if message.instance_id != self._instance.instance_id:
                raise ValueError("Control Plane sent a message for another Instance")
            await handler(message)

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._sleep(self._heartbeat_interval)
            self.enqueue_nowait(
                WireMessage(
                    type=MessageType.BRIDGE_HEARTBEAT,
                    instance_id=self._instance.instance_id,
                    payload=BridgeHeartbeatPayload().model_dump(mode="json"),
                )
            )
