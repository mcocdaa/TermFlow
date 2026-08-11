"""Agent Broker product conversation API tests (plan M1, task M1.5).

Covers the product-facing conversation endpoints under
``/api/v1/agent/conversations``: create/list/detail/delete, paginated
historical messages, paginated canonical events with a ``since`` cursor, and
the backend-opacity guarantee (product responses never expose backend-conversation
internals such as ``provider_ref``).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.persistence.repositories import RepositoryBundle


def _create_profile(
    client: TestClient,
    admin_headers: dict[str, str],
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model": "default"}',
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
    term = provision_term(name="conversation-term")
    binding = _create_binding(
        client,
        admin_headers,
        profile_id=UUID(str(profile["profile_id"])),
        term_id=term.instance_id,
    )
    return UUID(str(binding["binding_id"]))


class TestConversations:
    def test_create_list_paginate_detail(self, client, admin_headers, provision_term) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        first = _create_conversation(client, admin_headers, binding_id=binding_id, title="first")
        second = _create_conversation(client, admin_headers, binding_id=binding_id, title="second")
        first_id = UUID(str(first["conversation_id"]))
        second_id = UUID(str(second["conversation_id"]))

        listed = client.get(
            f"/api/v1/agent/conversations?binding_id={binding_id}",
            headers=admin_headers,
        )
        assert listed.status_code == 200
        conversations = listed.json()["conversations"]
        assert [c["conversation_id"] for c in conversations] == [
            str(first_id),
            str(second_id),
        ]
        assert all(c["binding_id"] == str(binding_id) for c in conversations)
        assert conversations[0]["status"] == "active"

        page = client.get(
            f"/api/v1/agent/conversations?binding_id={binding_id}&limit=1&offset=1",
            headers=admin_headers,
        )
        assert page.status_code == 200
        assert [c["conversation_id"] for c in page.json()["conversations"]] == [str(second_id)]

        detail = client.get(
            f"/api/v1/agent/conversations/{first_id}",
            headers=admin_headers,
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["conversation_id"] == str(first_id)
        assert body["title"] == "first"
        assert body["binding"]["binding_id"] == str(binding_id)
        assert body["binding"]["term_id"] is not None

    def test_create_requires_existing_binding(self, client, admin_headers) -> None:
        missing = client.post(
            "/api/v1/agent/conversations",
            headers=admin_headers,
            json={"binding_id": str(uuid4())},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "binding_not_found"

    def test_get_unknown_conversation_returns_404(self, client, admin_headers) -> None:
        assert (
            client.get(
                f"/api/v1/agent/conversations/{uuid4()}",
                headers=admin_headers,
            ).status_code
            == 404
        )

    def test_delete_conversation(self, client, admin_headers, provision_term) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client,
            admin_headers,
            binding_id=binding_id,
            title="ephemeral",
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        deleted = client.delete(
            f"/api/v1/agent/conversations/{conversation_id}",
            headers=admin_headers,
        )
        assert deleted.status_code == 204

        gone = client.get(
            f"/api/v1/agent/conversations/{conversation_id}",
            headers=admin_headers,
        )
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "conversation_not_found"


class TestMessages:
    def test_messages_are_paginated_and_ordered(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        repositories: RepositoryBundle = client.app.state.repositories
        for index in range(4):
            async def _seed_message(index: int = index) -> None:
                await repositories.agent_messages.create(
                    conversation_id=conversation_id,
                    role="user" if index % 2 == 0 else "agent",
                    kind="text",
                    body_digest=f"digest-{index}",
                    is_final=True,
                )

            client.portal.call(_seed_message)

        full = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            headers=admin_headers,
        )
        assert full.status_code == 200
        messages = full.json()["messages"]
        assert len(messages) == 4
        assert [m["body_digest"] for m in messages] == [
            "digest-0",
            "digest-1",
            "digest-2",
            "digest-3",
        ]
        assert [m["assembly_revision"] for m in messages] == [1, 2, 3, 4]
        assert all(m["conversation_id"] == str(conversation_id) for m in messages)

        page = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/messages?limit=2&offset=2",
            headers=admin_headers,
        )
        assert [m["body_digest"] for m in page.json()["messages"]] == ["digest-2", "digest-3"]

    def test_messages_for_unknown_conversation_return_404(
        self,
        client,
        admin_headers,
    ) -> None:
        missing = client.get(
            f"/api/v1/agent/conversations/{uuid4()}/messages",
            headers=admin_headers,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "conversation_not_found"


class TestEvents:
    def test_events_are_ordered_paginated_and_support_since_cursor(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        repositories: RepositoryBundle = client.app.state.repositories
        for index in range(4):
            async def _append_event(index: int = index) -> None:
                await repositories.agent_events.append(
                    conversation_id=conversation_id,
                    event_kind="message_committed",
                    dedup_key=f"dedup-{index}",
                    payload_digest=f"payload-{index}",
                )

            client.portal.call(_append_event)

        full = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
        )
        assert full.status_code == 200
        events = full.json()["events"]
        assert [e["database_seq"] for e in events] == [1, 2, 3, 4]
        assert events[0]["event_kind"] == "message_committed"
        assert full.json()["next_cursor"] == 4

        since = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events?since=2",
            headers=admin_headers,
        )
        assert since.status_code == 200
        assert [e["database_seq"] for e in since.json()["events"]] == [3, 4]
        assert since.json()["next_cursor"] == 4

        page = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events?limit=2&offset=2",
            headers=admin_headers,
        )
        assert [e["database_seq"] for e in page.json()["events"]] == [3, 4]

    def test_events_for_unknown_conversation_return_404(self, client, admin_headers) -> None:
        missing = client.get(
            f"/api/v1/agent/conversations/{uuid4()}/events",
            headers=admin_headers,
        )
        assert missing.status_code == 404


class TestProviderOpacity:
    def test_product_endpoints_never_expose_backend_internals(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        repositories: RepositoryBundle = client.app.state.repositories

        async def _seed_backend_ref() -> None:
            await repositories.agent_backend_conversations.create(
                conversation_id=conversation_id,
                backend_kind="opencode",
                runtime_id="runtime-1",
                binding_capability_epoch=1,
                provider_ref="opaque-backend-session-xyz",
            )

        async def _seed_message() -> None:
            await repositories.agent_messages.create(
                conversation_id=conversation_id,
                role="user",
                kind="text",
                body_digest="digest-opaque",
                is_final=True,
            )

        async def _seed_event() -> None:
            await repositories.agent_events.append(
                conversation_id=conversation_id,
                event_kind="message_committed",
                dedup_key="opaque-event",
                payload_digest="payload-opaque",
            )

        client.portal.call(_seed_backend_ref)
        client.portal.call(_seed_message)
        client.portal.call(_seed_event)

        detail = client.get(
            f"/api/v1/agent/conversations/{conversation_id}",
            headers=admin_headers,
        )
        messages = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            headers=admin_headers,
        )
        events = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
        )
        listed = client.get(
            f"/api/v1/agent/conversations?binding_id={binding_id}",
            headers=admin_headers,
        )

        for response in (detail, messages, events, listed):
            assert response.status_code == 200
            body = json.dumps(response.json())
            assert "provider_ref" not in body
            assert "opaque-backend-session-xyz" not in body
            assert "runtime_id" not in body
            assert "backend_kind" not in body
