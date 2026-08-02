import time
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    PaneSnapshot,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)


def _installation_token(client, admin_headers) -> str:
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    return client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment},
    ).json()["installation_token"]


def _register(client, installation_token: str, instance_id, name: str = "alpha") -> str:
    response = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation_token}"},
        json={"instance_id": str(instance_id), "name": name},
    )
    assert response.status_code == 201
    return response.json()["instance_token"]


def test_registration_retry_rotates_instance_token_for_owner(client, admin_headers) -> None:
    installation_token = _installation_token(client, admin_headers)
    instance_id = uuid4()
    first = _register(client, installation_token, instance_id)
    second = _register(client, installation_token, instance_id, "renamed")
    assert first != second


def test_bridge_rejects_invalid_token(client) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": "Bearer invalid"},
        ):
            pass
    assert caught.value.code == 4401


def test_retired_bridge_is_rejected_before_live_publish(client, admin_headers) -> None:
    installation_token = _installation_token(client, admin_headers)
    instance_id = uuid4()
    token = _register(client, installation_token, instance_id)
    client.portal.call(client.app.state.registry.begin_retirement, instance_id)

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {token}"},
        ):
            pass
    assert caught.value.code == 4401
    assert (
        client.portal.call(
            client.app.state.registry.maybe_get,
            instance_id,
        )
        is None
    )


def test_bridge_token_race_cleans_live_registration(
    client,
    admin_headers,
    monkeypatch,
) -> None:
    installation_token = _installation_token(client, admin_headers)
    instance_id = uuid4()
    token = _register(client, installation_token, instance_id)
    instances = client.app.state.repositories.instances
    original_get_by_token_hash = instances.get_by_token_hash
    lookup_count = 0

    async def raced_get_by_token_hash(token_hash):
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return await original_get_by_token_hash(token_hash)
        return None

    monkeypatch.setattr(instances, "get_by_token_hash", raced_get_by_token_hash)

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {token}"},
        ):
            pass
    assert caught.value.code == 4401
    assert lookup_count == 2
    assert (
        client.portal.call(
            client.app.state.registry.maybe_get,
            instance_id,
        )
        is None
    )


def test_bridge_authenticates_and_updates_live_topology(client, admin_headers) -> None:
    installation_token = _installation_token(client, admin_headers)
    instance_id = uuid4()
    instance_token = _register(client, installation_token, instance_id)
    topology = TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    )
                ],
            )
        ],
    )
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as websocket:
        hello = BridgeHelloPayload(name="alpha")
        websocket.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=hello.model_dump(mode="json"),
            ).model_dump_json()
        )
        websocket.send_text(
            WireMessage(
                type=MessageType.TOPOLOGY_SNAPSHOT,
                instance_id=instance_id,
                payload=TopologySnapshotPayload(topology=topology).model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = None
        for _ in range(100):
            connection = client.portal.call(registry.maybe_get, instance_id)
            if connection is not None and connection.topology is not None:
                break
            time.sleep(0.01)
        assert connection is not None
        assert connection.topology == topology
