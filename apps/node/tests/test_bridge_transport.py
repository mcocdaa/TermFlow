import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from termflow_node.bridge.backoff import ReconnectBackoff
from termflow_node.bridge.transport import BridgeTransport, bridge_websocket_url
from termflow_node.config.models import InstallationConfig
from termflow_node.control_plane_client import ControlPlaneClient
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_protocol import (
    MessageType,
    TerminalClosedPayload,
    TerminalOutputPayload,
    TopologySnapshot,
    WireMessage,
)


def _instance(tmp_path, *, token: str | None) -> LocalInstance:
    return LocalInstance(
        instance_id=uuid4(),
        name="work",
        socket_path=tmp_path / "tmux.sock",
        created_at=datetime.now(UTC),
        instance_token=SecretStr(token) if token else None,
        lifecycle=InstanceLifecycle.RUNNING,
    )


def _installation() -> InstallationConfig:
    return InstallationConfig.model_validate(
        {
            "server_url": "http://127.0.0.1:8000",
            "installation_id": str(uuid4()),
            "installation_token": "installation-secret",
        }
    )


def test_websocket_url_policy() -> None:
    assert bridge_websocket_url("https://example.com/base") == (
        "wss://example.com/base/api/v1/bridge/connect"
    )
    assert bridge_websocket_url("http://127.0.0.1:8000") == (
        "ws://127.0.0.1:8000/api/v1/bridge/connect"
    )
    with pytest.raises(ValueError):
        bridge_websocket_url("http://example.com")


@pytest.mark.asyncio
async def test_registration_persists_instance_token_before_connect(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance = _instance(tmp_path, token=None)
    store.save(instance)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer installation-secret"
        assert json.loads(request.content)["instance_id"] == str(instance.instance_id)
        return httpx.Response(
            201,
            json={
                "instance_id": str(instance.instance_id),
                "instance_token": "instance-token-that-is-long-enough-for-registration",
            },
        )

    client = ControlPlaneClient(transport=httpx.MockTransport(handler))
    updated = await client.register_instance(_installation(), instance, store)
    assert updated.instance_token is not None
    assert store.load(instance.instance_id).instance_token == updated.instance_token


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = asyncio.Event()

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def recv(self) -> str:
        await self.closed.wait()
        raise RuntimeError("closed")


class SerialCheckingWebSocket(FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.in_send = False
        self.max_concurrent = 0

    async def send(self, value: str) -> None:
        assert self.in_send is False
        self.in_send = True
        self.max_concurrent = max(self.max_concurrent, 1)
        await asyncio.sleep(0)
        self.sent.append(value)
        self.in_send = False


class ConnectionListener:
    def __init__(self) -> None:
        self.events: list[str] = []

    def bridge_connected(self) -> None:
        self.events.append("connected")

    def bridge_disconnected(self) -> None:
        self.events.append("disconnected")


@pytest.mark.asyncio
async def test_transport_sends_hello_then_full_topology_and_stops(tmp_path) -> None:
    websocket = FakeWebSocket()
    connection_arguments: dict[str, object] = {}

    @asynccontextmanager
    async def connect(uri: str, **kwargs):
        connection_arguments.update({"uri": uri, **kwargs})
        yield websocket

    instance = _instance(tmp_path, token="instance-secret-token")
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    topology = TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[],
    )
    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: topology,
        connect=connect,
        heartbeat_interval=0.01,
        backoff=ReconnectBackoff(base=0, cap=0),
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(transport.run(lambda message: asyncio.sleep(0), shutdown))
    for _ in range(100):
        if len(websocket.sent) >= 2:
            break
        await asyncio.sleep(0.005)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    messages = [WireMessage.model_validate_json(value) for value in websocket.sent]
    assert [message.type for message in messages[:2]] == [
        MessageType.BRIDGE_HELLO,
        MessageType.TOPOLOGY_SNAPSHOT,
    ]
    assert connection_arguments["ping_interval"] is None
    assert connection_arguments["additional_headers"] == {
        "Authorization": "Bearer instance-secret-token"
    }


@pytest.mark.asyncio
async def test_all_producers_share_one_serial_send_queue_and_lifecycle(tmp_path) -> None:
    websocket = SerialCheckingWebSocket()

    @asynccontextmanager
    async def connect(uri: str, **kwargs):
        yield websocket

    instance = _instance(tmp_path, token="instance-secret-token")
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    topology = TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[],
    )
    listener = ConnectionListener()
    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: topology,
        connect=connect,
        heartbeat_interval=0.01,
        backoff=ReconnectBackoff(base=0, cap=0),
    )
    transport.set_connection_listener(listener)
    shutdown = asyncio.Event()
    task = asyncio.create_task(transport.run(lambda message: asyncio.sleep(0), shutdown))
    for _ in range(100):
        if listener.events == ["connected"]:
            break
        await asyncio.sleep(0.001)
    for index in range(20):
        assert transport.enqueue_nowait(
            WireMessage(
                type=MessageType.TERMINAL_SIZE,
                instance_id=instance.instance_id,
                payload={"terminal_id": str(uuid4()), "rows": 24, "cols": 80 + index},
            )
        )
    for _ in range(100):
        if len(websocket.sent) >= 22:
            break
        await asyncio.sleep(0.005)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)
    assert websocket.max_concurrent == 1
    assert listener.events == ["connected", "disconnected"]


@pytest.mark.asyncio
async def test_terminal_teardown_has_reserved_transport_capacity(tmp_path) -> None:
    instance = _instance(tmp_path, token="instance-secret-token")
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: TopologySnapshot(
            session_id="$0", session_name="main", revision=1, windows=[]
        ),
        queue_size=1,
    )
    terminal_id = uuid4()
    stream_id = uuid4()
    output = WireMessage(
        type=MessageType.TERMINAL_OUTPUT,
        instance_id=instance.instance_id,
        payload=TerminalOutputPayload.from_bytes(
            terminal_id, stream_id, 1, b"full"
        ).model_dump(mode="json"),
    )
    closed = WireMessage(
        type=MessageType.TERMINAL_CLOSED,
        instance_id=instance.instance_id,
        payload=TerminalClosedPayload(
            terminal_id=terminal_id, reason="internal_error"
        ).model_dump(mode="json"),
    )

    assert transport.enqueue_nowait(output)
    assert not transport.enqueue_nowait(output)
    assert transport.enqueue_nowait(closed)
    replacement_id = uuid4()
    replacement = WireMessage(
        type=MessageType.TERMINAL_CLOSED,
        instance_id=instance.instance_id,
        payload=TerminalClosedPayload(
            terminal_id=replacement_id, reason="internal_error"
        ).model_dump(mode="json"),
    )
    assert transport.enqueue_nowait(replacement)
    assert (await transport._outbound.get()).type is MessageType.TERMINAL_OUTPUT
    latest_close = await transport._outbound.get()
    assert latest_close.type is MessageType.TERMINAL_CLOSED
    assert latest_close.payload["terminal_id"] == str(replacement_id)
    assert transport._outbound.empty()
