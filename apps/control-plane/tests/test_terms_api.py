from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import MessageType, TermRenameResultPayload, WireMessage


def test_offline_term_rename_is_rejected_without_persistence(
    client,
    admin_headers,
    provision_term,
) -> None:
    instance_id = provision_term(hostname="term-host", name="before").instance_id
    response = client.patch(
        f"/api/v1/terms/{instance_id}",
        headers=admin_headers,
        json={"name": "never-queued"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "instance_offline"
    stored = client.portal.call(client.app.state.repositories.instances.get, instance_id)
    assert stored.name == "before"


def test_online_term_rename_persists_only_after_bridge_success(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(hostname="term-host", name="before")
    instance_id = term.instance_id
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
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


def test_failed_bridge_rename_keeps_last_known_name(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(hostname="term-host", name="before")
    instance_id = term.instance_id
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
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


def test_offline_delete_revokes_token_keeps_computer_and_allows_fresh_registration(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(hostname="term-host", name="before")
    instance_id = term.instance_id
    old_token = term.instance_token
    installation_token = term.computer.installation_token
    client.portal.call(
        client.app.state.repositories.audit.record,
        "term.before-delete",
        instance_id,
        None,
        None,
        "ok",
        None,
    )

    response = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
    assert response.status_code == 204
    assert (
        client.portal.call(
            client.app.state.repositories.instances.get,
            instance_id,
        )
        is None
    )

    dashboard = client.get("/api/v1/dashboard", headers=admin_headers).json()
    assert dashboard["metrics"]["total_terms"] == 0
    assert dashboard["metrics"]["computers"] == 1
    assert dashboard["computers"][0]["terms"] == []
    audits = client.portal.call(client.app.state.repositories.audit.list_all)
    assert [audit.operation for audit in audits] == [
        "term.before-delete",
        "term.delete",
    ]
    assert all(audit.instance_id == instance_id for audit in audits)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {old_token}"},
        ):
            pass
    assert rejected.value.code == 4401

    replacement = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation_token}"},
        json={"instance_id": str(instance_id), "name": "before"},
    )
    assert replacement.status_code == 201
    new_token = replacement.json()["instance_token"]
    assert new_token != old_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {new_token}"},
    ):
        pass


def test_unknown_and_already_retiring_terms_return_not_found(
    client,
    admin_headers,
    provision_term,
) -> None:
    unknown = client.delete(f"/api/v1/terms/{uuid4()}", headers=admin_headers)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "instance_not_found"

    instance_id = provision_term(hostname="term-host", name="before").instance_id
    client.portal.call(client.app.state.registry.begin_retirement, instance_id)
    retiring = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
    assert retiring.status_code == 404
    assert retiring.json()["error"]["code"] == "instance_not_found"
    assert (
        client.portal.call(
            client.app.state.repositories.instances.get,
            instance_id,
        )
        is not None
    )


def test_online_term_is_not_deleted(client, admin_headers, provision_term) -> None:
    term = provision_term(hostname="term-host", name="before")
    instance_id = term.instance_id
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
    ):
        online = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
        assert online.status_code == 409
        assert online.json()["error"]["code"] == "instance_online"
    assert (
        client.portal.call(
            client.app.state.repositories.instances.get,
            instance_id,
        )
        is not None
    )
