from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    MessageType,
    PaneOutputPayload,
    WireMessage,
)


def _provision_instance(client, admin_headers):
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment},
    ).json()
    instance_id = uuid4()
    registration = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "events"},
    ).json()
    return instance_id, registration["instance_token"]


def test_events_reject_invalid_admin_token(client) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            f"/api/v1/events?instance_id={uuid4()}",
            headers={"Authorization": "Bearer invalid"},
        ):
            pass
    assert caught.value.code == 4401


def test_matching_pane_output_is_forwarded(client, admin_headers) -> None:
    instance_id, instance_token = _provision_instance(client, admin_headers)
    event_url = f"/api/v1/events?instance_id={instance_id}"
    with client.websocket_connect(event_url, headers=admin_headers) as events:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {instance_token}"},
        ) as bridge:
            output = PaneOutputPayload.from_bytes("%1", uuid4(), 1, b"hello\xff")
            bridge.send_text(
                WireMessage(
                    type=MessageType.PANE_OUTPUT,
                    instance_id=instance_id,
                    payload=output.model_dump(mode="json"),
                ).model_dump_json()
            )
            received = WireMessage.model_validate(events.receive_json())
            if received.type is MessageType.INSTANCE_ONLINE:
                received = WireMessage.model_validate(events.receive_json())
            assert received.type is MessageType.PANE_OUTPUT
            assert received.instance_id == instance_id
            assert PaneOutputPayload.model_validate(received.payload).to_bytes() == b"hello\xff"


def test_replay_cursor_enqueues_request_to_bridge(client, admin_headers) -> None:
    instance_id, instance_token = _provision_instance(client, admin_headers)
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
        headers={"Authorization": f"Bearer {instance_token}"},
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
