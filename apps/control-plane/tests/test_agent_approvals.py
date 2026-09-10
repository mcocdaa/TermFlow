"""Approval Request API tests (plan §12.1, task M5.1).

Exercises the endpoints under ``/api/v1/agent/approvals``: decide
(approve/deny with a 409 double-decision guard), revoke (with a 409
fail-closed decide afterwards), and conversation-scoped listing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalArgsHashInput,
    canonical_hash,
)

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"
ORIGIN = "http://127.0.0.1:8000"


def _hash_input(**overrides: object) -> ApprovalArgsHashInput:
    """A fully populated canonical-hash input; overrides replace any field."""
    values: dict[str, object] = {
        "schema_version": 1,
        "operation": "key",
        "instance_id": uuid4(),
        "pane_id": "p1",
        "pane_incarnation": "incarnation-1",
        "encoded_bytes": b"\x1b[A",
        "submit": False,
        "cursor_precondition": "cursor-42",
        "run_id": uuid4(),
        "grant_id": None,
        "expiry": datetime.now(UTC) + timedelta(minutes=5),
        "policy_epoch": 1,
    }
    values.update(overrides)
    return ApprovalArgsHashInput(**values)


def _create_profile(client: TestClient, admin_headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    profile_id: UUID,
    term_id: UUID,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": str(profile_id), "term_id": str(term_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_conversation(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    binding_id: UUID,
    title: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"binding_id": str(binding_id)}
    if title is not None:
        body["title"] = title
    response = client.post("/api/v1/agent/conversations", headers=admin_headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> UUID:
    profile = _create_profile(client, admin_headers)
    term = provision_term(name="approval-term")
    binding = _create_binding(
        client,
        admin_headers,
        profile_id=UUID(str(profile["profile_id"])),
        term_id=term.instance_id,
    )
    return UUID(str(binding["binding_id"]))


def _seed_approval(
    client: TestClient,
    *,
    binding_id: UUID,
    conversation_id: UUID,
    tool_call_id: str = "tool-call-1",
    expires_at: datetime | None = None,
    auth_epoch: int = 1,
) -> UUID:
    repositories: RepositoryBundle = client.app.state.repositories

    async def _create() -> UUID:
        approval = await repositories.approvals.create(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=auth_epoch,
            expires_at=expires_at or (datetime.now(UTC) + timedelta(minutes=5)),
        )
        return approval.id

    return client.portal.call(_create)


def _authenticate_browser(client: TestClient) -> None:
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert response.status_code == 201, response.text


def test_decide_approve_and_deny_via_api_with_double_decision_guard(
    client, admin_headers, provision_term
) -> None:
    assert client.get(f"/api/v1/agent/approvals/{uuid4()}").status_code == 401

    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    conversation_id = UUID(str(conversation["conversation_id"]))
    approval_id = _seed_approval(
        client, binding_id=binding_id, conversation_id=conversation_id
    )

    _authenticate_browser(client)
    approved = client.post(
        f"/api/v1/agent/approvals/{approval_id}/decide",
        headers={"Origin": ORIGIN},
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["decision"] == "approved"

    second = client.post(
        f"/api/v1/agent/approvals/{approval_id}/decide",
        headers=admin_headers,
        json={"decision": "deny"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "approval_already_decided"

    denied_id = _seed_approval(
        client,
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id="tool-call-2",
    )
    denied = client.post(
        f"/api/v1/agent/approvals/{denied_id}/decide",
        headers=admin_headers,
        json={"decision": "deny"},
    )
    assert denied.status_code == 200
    assert denied.json()["state"] == "denied"
    assert denied.json()["decision"] == "denied"


def test_approve_requires_fresh_auth_but_safe_deny_does_not(
    client, admin_headers, provision_term
) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    conversation_id = UUID(str(conversation["conversation_id"]))
    approval_id = _seed_approval(
        client, binding_id=binding_id, conversation_id=conversation_id
    )

    blocked = client.post(
        f"/api/v1/agent/approvals/{approval_id}/decide",
        headers=admin_headers,
        json={"decision": "approve"},
    )
    assert blocked.status_code == 428, blocked.text
    assert blocked.json()["error"]["code"] == "approval_reauthentication_required"

    unchanged = client.get(
        f"/api/v1/agent/approvals/{approval_id}",
        headers=admin_headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["state"] == "pending"

    denied = client.post(
        f"/api/v1/agent/approvals/{approval_id}/decide",
        headers=admin_headers,
        json={"decision": "deny"},
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["state"] == "denied"


def test_fresh_browser_auth_can_approve(client, admin_headers, provision_term) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    approval_id = _seed_approval(
        client,
        binding_id=binding_id,
        conversation_id=UUID(str(conversation["conversation_id"])),
    )

    _authenticate_browser(client)
    approved = client.post(
        f"/api/v1/agent/approvals/{approval_id}/decide",
        headers={"Origin": ORIGIN},
        json={"decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"


def test_revoke_and_conversation_listing_via_api(
    client, admin_headers, provision_term
) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    first_conversation = _create_conversation(
        client, admin_headers, binding_id=binding_id, title="first"
    )
    second_conversation = _create_conversation(
        client, admin_headers, binding_id=binding_id, title="second"
    )
    first_id = UUID(str(first_conversation["conversation_id"]))
    second_id = UUID(str(second_conversation["conversation_id"]))

    approval_one = _seed_approval(
        client, binding_id=binding_id, conversation_id=first_id
    )
    approval_two = _seed_approval(
        client,
        binding_id=binding_id,
        conversation_id=first_id,
        tool_call_id="tool-call-2",
    )
    _seed_approval(
        client,
        binding_id=binding_id,
        conversation_id=second_id,
        tool_call_id="tool-call-3",
    )

    revoked = client.post(
        f"/api/v1/agent/approvals/{approval_one}/revoke",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["decision"] == "revoked"

    _authenticate_browser(client)
    decided = client.post(
        f"/api/v1/agent/approvals/{approval_one}/decide",
        headers={"Origin": ORIGIN},
        json={"decision": "approve"},
    )
    assert decided.status_code == 409
    assert decided.json()["error"]["code"] == "approval_revoked"

    listed = client.get(
        f"/api/v1/agent/approvals?conversation_id={first_id}",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    approval_ids = [item["approval_id"] for item in listed.json()["approvals"]]
    assert set(approval_ids) == {str(approval_one), str(approval_two)}

    all_approvals = client.get("/api/v1/agent/approvals", headers=admin_headers)
    assert all_approvals.status_code == 200
    assert len(all_approvals.json()["approvals"]) == 3

    unknown = client.get(
        f"/api/v1/agent/approvals?conversation_id={uuid4()}",
        headers=admin_headers,
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "conversation_not_found"
