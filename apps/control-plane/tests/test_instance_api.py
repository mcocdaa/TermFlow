import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    PaneSnapshot,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)


def _topology() -> TopologySnapshot:
    return TopologySnapshot(
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


def test_admin_can_list_and_read_instances(client, admin_headers, provision_term) -> None:
    instance_id = provision_term(name="work").instance_id
    listing = client.get("/api/v1/instances", headers=admin_headers)
    assert listing.status_code == 200
    assert listing.json()["instances"][0]["instance_id"] == str(instance_id)
    assert listing.json()["instances"][0]["online"] is False

    detail = client.get(f"/api/v1/instances/{instance_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["name"] == "work"


def test_offline_topology_and_input_are_rejected(
    client,
    admin_headers,
    provision_term,
) -> None:
    instance_id = provision_term(name="work").instance_id
    topology = client.get(
        f"/api/v1/instances/{instance_id}/topology",
        headers=admin_headers,
    )
    assert topology.status_code == 409
    assert topology.json()["error"]["code"] == "instance_offline"

    response = client.post(
        f"/api/v1/instances/{instance_id}/panes/%251/input",
        headers={**admin_headers, "Idempotency-Key": str(uuid4())},
        json={"text": "not persisted", "submit": False},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "instance_offline"
    audit = client.portal.call(client.app.state.repositories.audit.list_all)
    assert audit[-1].input_bytes == len(b"not persisted")
    assert not hasattr(audit[-1], "text")


def test_http_input_waits_for_bridge_confirmation(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(name="work")
    instance_id = term.instance_id
    topology = _topology()
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
    ) as websocket:
        websocket.send_text(
            WireMessage(
                type=MessageType.TOPOLOGY_SNAPSHOT,
                instance_id=instance_id,
                payload=TopologySnapshotPayload(topology=topology).model_dump(mode="json"),
            ).model_dump_json()
        )
        for _ in range(100):
            connection = client.portal.call(client.app.state.registry.maybe_get, instance_id)
            if connection is not None and connection.topology is not None:
                break
            time.sleep(0.01)

        idempotency_key = uuid4()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending_response = executor.submit(
                client.post,
                f"/api/v1/instances/{instance_id}/panes/%251/input",
                headers={**admin_headers, "Idempotency-Key": str(idempotency_key)},
                json={"text": "继续", "submit": True},
            )
            incoming = WireMessage.model_validate(websocket.receive_json())
            assert incoming.type is MessageType.PANE_INPUT
            assert incoming.payload["text"] == "继续"
            command_id = UUID(str(incoming.payload["command_id"]))
            websocket.send_text(
                WireMessage(
                    type=MessageType.COMMAND_RESULT,
                    instance_id=instance_id,
                    payload=CommandResultPayload(
                        command_id=command_id,
                        idempotency_key=idempotency_key,
                        ok=True,
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            response = pending_response.result(timeout=2)
    assert response.status_code == 200
    assert response.json() == {
        "command_id": str(command_id),
        "idempotency_key": str(idempotency_key),
        "ok": True,
    }


def test_control_character_is_rejected_at_http_boundary(
    client,
    admin_headers,
    provision_term,
) -> None:
    instance_id = provision_term(name="work").instance_id
    response = client.post(
        f"/api/v1/instances/{instance_id}/panes/%251/input",
        headers={**admin_headers, "Idempotency-Key": str(uuid4())},
        json={"text": "stop\u0003", "submit": False},
    )
    assert response.status_code == 422
