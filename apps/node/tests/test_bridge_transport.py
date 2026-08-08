import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from termflow_node.bridge.backoff import ReconnectBackoff
from termflow_node.bridge.transport import (
    BridgeTransport,
    bridge_websocket_url,
    is_instance_credential_rejection,
)
from termflow_node.config.models import InstallationConfig
from termflow_node.control_plane_client import ControlPlaneClient
from termflow_node.instances.models import (
    InstanceLifecycle,
    LocalInstance,
    RemoteAccessState,
)
from termflow_node.instances.store import InstanceStore
from termflow_protocol import (
    MessageType,
    TerminalClosedPayload,
    TerminalOutputPayload,
    TopologySnapshot,
    WireMessage,
)
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response


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
    assert bridge_websocket_url("http://192.168.0.53:8765", allow_insecure_http=True) == (
        "ws://192.168.0.53:8765/api/v1/bridge/connect"
    )
    with pytest.raises(ValueError):
        bridge_websocket_url("http://example.com")
    with pytest.raises(ValueError):
        bridge_websocket_url("http://192.168.0.53:8765")


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
        payload=TerminalOutputPayload.from_bytes(terminal_id, stream_id, 1, b"full").model_dump(
            mode="json"
        ),
    )
    closed = WireMessage(
        type=MessageType.TERMINAL_CLOSED,
        instance_id=instance.instance_id,
        payload=TerminalClosedPayload(terminal_id=terminal_id, reason="internal_error").model_dump(
            mode="json"
        ),
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


def _rejection(status_code: int) -> InvalidStatus:
    return InvalidStatus(Response(status_code, "Rejected", Headers()))


@pytest.mark.parametrize(
    "error",
    [
        _rejection(403),
        ConnectionClosedError(Close(4401, "Authentication required"), None),
    ],
)
def test_credential_rejection_classification_is_exact(error: Exception) -> None:
    assert is_instance_credential_rejection(error)


@pytest.mark.parametrize(
    "error",
    [
        _rejection(401),
        _rejection(500),
        ConnectionClosedError(Close(4403, "Forbidden"), None),
        OSError("network unavailable"),
    ],
)
def test_transient_or_non_instance_rejections_are_not_definitive(error: Exception) -> None:
    assert not is_instance_credential_rejection(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _rejection(403),
        ConnectionClosedError(Close(4401, "Authentication required"), None),
    ],
)
async def test_auth_rejection_requires_activation_and_stops_retry(
    tmp_path, error: Exception
) -> None:
    instance = _instance(tmp_path, token="deleted-token").model_copy(
        update={"schema_version": 3, "session_id": "$0", "bridge_pid": 4321}
    )
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    sleeps: list[float] = []
    shutdown = asyncio.Event()

    @asynccontextmanager
    async def rejected_connect(uri: str, **kwargs):
        raise error
        yield

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        shutdown.set()

    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: TopologySnapshot(
            session_id="$0", session_name="main", revision=1, windows=[]
        ),
        connect=rejected_connect,
        sleep=record_sleep,
    )

    await transport.run(lambda message: asyncio.sleep(0), shutdown)

    saved = store.load(instance.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    assert saved.instance_token is None
    assert saved.bridge_pid is None
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("offline"), _rejection(503)])
async def test_transient_failures_retry_without_changing_remote_access(
    tmp_path, error: Exception
) -> None:
    instance = _instance(tmp_path, token="still-valid").model_copy(
        update={"schema_version": 3, "session_id": "$0", "bridge_pid": 4321}
    )
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    shutdown = asyncio.Event()
    sleeps: list[float] = []

    @asynccontextmanager
    async def rejected_connect(uri: str, **kwargs):
        raise error
        yield

    async def stop_after_backoff(delay: float) -> None:
        sleeps.append(delay)
        shutdown.set()

    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: TopologySnapshot(
            session_id="$0", session_name="main", revision=1, windows=[]
        ),
        connect=rejected_connect,
        backoff=ReconnectBackoff(base=0, cap=0),
        sleep=stop_after_backoff,
    )

    await transport.run(lambda message: asyncio.sleep(0), shutdown)

    saved = store.load(instance.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVE
    assert saved.instance_token is not None
    assert saved.bridge_pid == 4321
    assert sleeps == [0]


@pytest.mark.asyncio
async def test_activation_required_never_registers_automatically(tmp_path) -> None:
    shutdown = asyncio.Event()
    registrations = 0

    class RejectRegistration:
        async def register_instance(self, *args, **kwargs):
            nonlocal registrations
            registrations += 1
            shutdown.set()
            raise OSError("registration must be explicit")

    instance = _instance(tmp_path, token=None).model_copy(
        update={
            "schema_version": 3,
            "session_id": "$0",
            "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
        }
    )
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    connect_called = False

    @asynccontextmanager
    async def connect(uri: str, **kwargs):
        nonlocal connect_called
        connect_called = True
        yield FakeWebSocket()

    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=RejectRegistration(),
        topology_provider=lambda: TopologySnapshot(
            session_id="$0", session_name="main", revision=1, windows=[]
        ),
        connect=connect,
    )

    await transport.run(lambda message: asyncio.sleep(0), shutdown)

    assert registrations == 0
    assert connect_called is False
    assert store.load(instance.instance_id).remote_access is RemoteAccessState.ACTIVATION_REQUIRED
