"""Product-facing Agent conversation title mutation contract tests."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> UUID:
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    assert profile.status_code == 201, profile.text
    term = provision_term(name="rename-term")
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": profile.json()["profile_id"], "term_id": str(term.instance_id)},
    )
    assert binding.status_code == 201, binding.text
    return UUID(str(binding.json()["binding_id"]))


def _create_conversation(
    client: TestClient,
    admin_headers: dict[str, str],
    binding_id: UUID,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        json={"binding_id": str(binding_id), "title": "初始标题"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_conversation_title_can_be_renamed_and_is_trimmed(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id)

    response = client.patch(
        f"/api/v1/agent/conversations/{conversation['conversation_id']}",
        headers=admin_headers,
        json={"title": "  部署检查  "},
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "部署检查"
    assert response.json()["conversation_id"] == conversation["conversation_id"]

    listed = client.get(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        params={"binding_id": str(binding_id)},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["conversations"][0]["title"] == "部署检查"


def test_conversation_title_rejects_blank_overlong_and_extra_fields(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id)
    url = f"/api/v1/agent/conversations/{conversation['conversation_id']}"

    for payload in (
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 256},
        {"title": "合法", "unexpected": True},
    ):
        response = client.patch(url, headers=admin_headers, json=payload)
        assert response.status_code == 422, (payload, response.text)


def test_renaming_missing_conversation_returns_product_error(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.patch(
        f"/api/v1/agent/conversations/{uuid4()}",
        headers=admin_headers,
        json={"title": "不存在"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"
