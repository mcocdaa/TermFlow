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
from termflow_protocol import MessageType, TopologySnapshot, WireMessage


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
