import time
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    PaneOutputPayload,
    PaneSnapshot,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)


def test_events_reject_invalid_admin_token(client) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            f"/api/v1/events?instance_id={uuid4()}",
            headers={"Authorization": "Bearer invalid"},
        ) as rejected:
            rejected.receive_json()
    assert caught.value.code == 4401


def test_matching_pane_output_is_forwarded(client, admin_headers, provision_term) -> None:
    term = provision_term(name="events")
    instance_id = term.instance_id
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
                        pane_id="%1",
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
    event_url = f"/api/v1/events?instance_id={instance_id}"
    with client.websocket_connect(event_url, headers=admin_headers) as events:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {term.instance_token}"},
        ) as bridge:
            bridge.send_text(
                WireMessage(
                    type=MessageType.BRIDGE_HELLO,
                    instance_id=instance_id,
                    payload=BridgeHelloPayload(name="events").model_dump(mode="json"),
                ).model_dump_json()
            )
            bridge.send_text(
                WireMessage(
                    type=MessageType.TOPOLOGY_SNAPSHOT,
                    instance_id=instance_id,
                    payload=TopologySnapshotPayload(topology=topology).model_dump(
                        mode="json"
                    ),
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

            output = PaneOutputPayload.from_bytes("%1", uuid4(), 1, b"hello\xff")
            bridge.send_text(
                WireMessage(
                    type=MessageType.PANE_OUTPUT,
                    instance_id=instance_id,
                    payload=output.model_dump(mode="json"),
                ).model_dump_json()
            )
            received = None
            for _ in range(6):
                candidate = WireMessage.model_validate(events.receive_json())
                if candidate.type is MessageType.PANE_OUTPUT:
                    received = candidate
                    break
            assert received is not None
            assert received.type is MessageType.PANE_OUTPUT
            assert received.instance_id == instance_id
            assert PaneOutputPayload.model_validate(received.payload).to_bytes() == b"hello\xff"


def test_replay_cursor_enqueues_request_to_bridge(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(name="events")
    instance_id = term.instance_id
    stream_id = uuid4()
    query = urlencode(
        {
            "instance_id": str(instance_id),
            "pane_id": "%1",
            "stream_id": str(stream_id),
            "after_seq": "7",
        }
    )
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
    ) as bridge:
        with client.websocket_connect(f"/api/v1/events?{query}", headers=admin_headers):
            request = WireMessage.model_validate(bridge.receive_json())
            assert request.type is MessageType.PANE_REPLAY_REQUEST
            assert request.payload == {
                "pane_id": "%1",
                "stream_id": str(stream_id),
                "after_seq": 7,
            }
            assert UUID(str(request.instance_id)) == instance_id


def test_browser_cookie_requires_exact_origin_for_event_websocket(
    client,
    admin_headers,
    provision_term,
) -> None:
    instance_id = provision_term(name="events").instance_id
    login = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": "http://127.0.0.1:8000"},
        json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
    )
    assert login.status_code == 201

    with client.websocket_connect(
        f"/api/v1/events?instance_id={instance_id}",
        headers={"Origin": "http://127.0.0.1:8000"},
    ):
        pass

    for headers, expected_code in (
        ({}, 4401),
        ({"Origin": "https://evil.example"}, 4403),
    ):
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(
                f"/api/v1/events?instance_id={instance_id}",
                headers=headers,
            ) as rejected:
                rejected.receive_json()
        assert caught.value.code == expected_code
