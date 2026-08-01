from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from termflow_protocol import MessageType, TermRenameResultPayload, WireMessage


def _provision_term(client, admin_headers):
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment, "hostname": "term-host"},
    ).json()
    instance_id = uuid4()
    registration = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "before"},
    ).json()
    return instance_id, registration["instance_token"]


def test_offline_term_rename_is_rejected_without_persistence(client, admin_headers) -> None:
    instance_id, _ = _provision_term(client, admin_headers)
    response = client.patch(
        f"/api/v1/terms/{instance_id}",
        headers=admin_headers,
        json={"name": "never-queued"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "instance_offline"
    stored = client.portal.call(client.app.state.repositories.instances.get, instance_id)
    assert stored.name == "before"


def test_online_term_rename_persists_only_after_bridge_success(client, admin_headers) -> None:
    instance_id, instance_token = _provision_term(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                client.patch,
                f"/api/v1/terms/{instance_id}",
                headers=admin_headers,
                json={"name": "after"},
            )
            command = WireMessage.model_validate(bridge.receive_json())
            assert command.type is MessageType.TERM_RENAME
            assert command.payload["name"] == "after"
            before_result = client.portal.call(
                client.app.state.repositories.instances.get,
                instance_id,
            )
            assert before_result.name == "before"
            command_id = UUID(str(command.payload["command_id"]))
            bridge.send_text(
                WireMessage(
                    type=MessageType.TERM_RENAME_RESULT,
                    instance_id=instance_id,
                    payload=TermRenameResultPayload(
                        command_id=command_id,
                        ok=True,
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            response = pending.result(timeout=2)

    assert response.status_code == 200
    assert response.json()["name"] == "after"
    stored = client.portal.call(client.app.state.repositories.instances.get, instance_id)
    assert stored.name == "after"
    audits = client.portal.call(client.app.state.repositories.audit.list_all)
    rename_audit = audits[-1]
    assert rename_audit.operation == "term.rename"
    assert rename_audit.input_bytes is None
    assert "after" not in repr(rename_audit)


def test_failed_bridge_rename_keeps_last_known_name(client, admin_headers) -> None:
    instance_id, instance_token = _provision_term(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                client.patch,
                f"/api/v1/terms/{instance_id}",
                headers=admin_headers,
                json={"name": "rejected"},
            )
            command = WireMessage.model_validate(bridge.receive_json())
            bridge.send_text(
                WireMessage(
                    type=MessageType.TERM_RENAME_RESULT,
                    instance_id=instance_id,
                    payload=TermRenameResultPayload(
                        command_id=UUID(str(command.payload["command_id"])),
                        ok=False,
                        error_code="rename_failed",
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            response = pending.result(timeout=2)
    assert response.status_code == 409
    stored = client.portal.call(client.app.state.repositories.instances.get, instance_id)
    assert stored.name == "before"
