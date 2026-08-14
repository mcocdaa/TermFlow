"""Agent Broker product conversation API tests (plan M1, task M1.5).

Covers the product-facing conversation endpoints under
``/api/v1/agent/conversations``: create/list/detail/delete, paginated
historical messages, paginated canonical events with a ``since`` cursor, and
the backend-opacity guarantee (product responses never expose backend-conversation
internals such as ``provider_ref``).
"""

from __future__ import annotations

import hashlib
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
            body = f"body-{index}" if index % 2 == 0 else None
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else f"digest-{index}"

            async def _seed_message(
                body: str | None = body, digest: str = digest, index: int = index
            ) -> None:
                await repositories.agent_messages.create(
                    conversation_id=conversation_id,
                    role="user" if index % 2 == 0 else "agent",
                    kind="text",
                    body_digest=digest,
                    is_final=True,
                    body=body,
                )

            client.portal.call(_seed_message)

        full = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            headers=admin_headers,
        )
        assert full.status_code == 200
        messages = full.json()["messages"]
        assert len(messages) == 4
        # Digest-only rows keep their plain digest; rows with a body carry the
        # digest of that body (the repository invariant).
        assert [m["body_digest"] for m in messages] == [
            hashlib.sha256(b"body-0").hexdigest(),
            "digest-1",
            hashlib.sha256(b"body-2").hexdigest(),
            "digest-3",
        ]
        # The M6b §6.6 body field is exposed per message; digest-only rows
        # (assistant deltas here) degrade to null.
        assert [m["body"] for m in messages] == ["body-0", None, "body-2", None]
        assert [m["assembly_revision"] for m in messages] == [1, 2, 3, 4]
        assert all(m["conversation_id"] == str(conversation_id) for m in messages)

        page = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/messages?limit=2&offset=2",
            headers=admin_headers,
        )
        assert [m["body"] for m in page.json()["messages"]] == ["body-2", None]
        assert [m["body_digest"] for m in page.json()["messages"]] == [
            hashlib.sha256(b"body-2").hexdigest(),
            "digest-3",
        ]

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


class TestEventsAGUI:
    """/events?wire=agui REST replay (plan M6a spec §4.5, test matrix §8).

    The envelope is unchanged (``events``/``next_cursor``, cursor semantics
    stay database_seq); only the event objects are AG-UI projections, and
    the stateless per-page projection keeps a message chunk+END pair
    complete across pages.
    """

    @staticmethod
    def _chunk_payload(message_id: UUID, text: str = "hello") -> str:
        return json.dumps(
            {
                "message_id": str(message_id),
                "part_id": "part-1",
                "assembly_revision": 1,
                "text": text,
                "ephemeral": True,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _end_payload(message_id: UUID) -> str:
        return json.dumps(
            {"message_id": str(message_id), "assembly_revision": 1, "final": True},
            separators=(",", ":"),
        )

    def _append(
        self,
        client: TestClient,
        conversation_id: UUID,
        *,
        kind: str,
        payload: str,
    ) -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        async def _append_event() -> None:
            await repositories.agent_events.append(
                conversation_id=conversation_id,
                event_kind=kind,
                dedup_key=f"agui-{kind}-{uuid4()}",
                payload_digest=digest,
                payload_json=payload,
            )

        client.portal.call(_append_event)

    def test_unknown_wire_value_is_rejected(self, client, admin_headers) -> None:
        response = client.get(
            f"/api/v1/agent/conversations/{uuid4()}/events",
            headers=admin_headers,
            params={"wire": "bogus"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_wire"

    def test_agui_events_are_projected_with_unchanged_next_cursor(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        message_id = uuid4()
        self._append(client, conversation_id, kind="message_delta",
                     payload=self._chunk_payload(message_id))
        self._append(client, conversation_id, kind="message_completed",
                     payload=self._end_payload(message_id))

        response = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
            params={"wire": "agui"},
        )
        assert response.status_code == 200
        body = response.json()
        events = body["events"]
        # The chunk+END pair is complete for the same messageId.
        assert events[0]["type"] == "TEXT_MESSAGE_CHUNK"
        assert events[0]["messageId"] == str(message_id)
        assert events[0]["role"] == "assistant"
        assert events[0]["delta"] == "hello"
        assert isinstance(events[0]["timestamp"], int)
        assert events[1] == {
            "type": "TEXT_MESSAGE_END",
            "messageId": str(message_id),
            "timestamp": events[1]["timestamp"],
        }
        # next_cursor keeps its database_seq semantics.
        assert body["next_cursor"] == 2

    def test_agui_pagination_projects_each_page_independently(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        message_ids = [uuid4() for _ in range(3)]
        for index, message_id in enumerate(message_ids):
            self._append(
                client, conversation_id, kind="message_delta",
                payload=self._chunk_payload(message_id, f"chunk-{index}"),
            )

        page_one = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
            params={"wire": "agui", "limit": 2, "offset": 0},
        )
        assert page_one.status_code == 200
        first = page_one.json()
        assert [event["messageId"] for event in first["events"]] == [
            str(message_ids[0]),
            str(message_ids[1]),
        ]
        assert first["next_cursor"] == 2

        page_two = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
            params={"wire": "agui", "since": 2},
        )
        assert page_two.status_code == 200
        second = page_two.json()
        assert [event["messageId"] for event in second["events"]] == [
            str(message_ids[2])
        ]
        assert second["next_cursor"] == 3

    def test_agui_drops_unprojectable_events_and_keeps_cursor_semantics(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        repositories: RepositoryBundle = client.app.state.repositories

        # A payload-less event (dropped by the projection) followed by a
        # projectable chunk.
        async def _append_payload_less() -> None:
            await repositories.agent_events.append(
                conversation_id=conversation_id,
                event_kind="message_delta",
                dedup_key="agui-no-payload",
                payload_digest="digest",
            )

        client.portal.call(_append_payload_less)
        message_id = uuid4()
        self._append(client, conversation_id, kind="message_delta",
                     payload=self._chunk_payload(message_id, "kept"))

        response = client.get(
            f"/api/v1/agent/conversations/{conversation_id}/events",
            headers=admin_headers,
            params={"wire": "agui"},
        )
        assert response.status_code == 200
        body = response.json()
        # Only the projectable event appears, but the cursor reflects the
        # last stored database_seq (2), not the number of emitted events.
        assert len(body["events"]) == 1
        assert body["events"][0]["messageId"] == str(message_id)
        assert body["next_cursor"] == 2


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
