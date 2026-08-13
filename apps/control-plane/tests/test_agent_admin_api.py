"""Agent Broker administration API tests (plan M1, task M1.5).

Covers the admin-only Profile, Binding, and AgentToken endpoints under
``/api/v1/agent/admin``: full CRUD, binding status transitions, raw-token
one-time issuance (only the hash is stored at rest), token revocation, and
auth enforcement (unauthenticated -> 401, non-admin -> 403).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.persistence.repositories import RepositoryBundle, decode_scopes

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"


def _create_profile(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    name: str = "opencode",
    backend_kind: str = "opencode",
    config: str = '{"model": "default"}',
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": name,
            "backend_kind": backend_kind,
            "config": config,
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


def _create_token(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    binding_id: UUID,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/tokens",
        headers=admin_headers,
        json={
            "binding_id": str(binding_id),
            "scopes": scopes or ["observe"],
            "expires_at": (expires_at or datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestProfiles:
    def test_create_list_get_patch_delete(self, client, admin_headers) -> None:
        created = _create_profile(client, admin_headers, name="primary")
        profile_id = UUID(str(created["profile_id"]))
        assert created["display_name"] == "primary"
        assert created["backend_kind"] == "opencode"
        assert created["config"] == '{"model": "default"}'

        listed = client.get("/api/v1/agent/admin/profiles", headers=admin_headers)
        assert listed.status_code == 200
        profiles = listed.json()["profiles"]
        assert any(profile["profile_id"] == str(profile_id) for profile in profiles)

        detail = client.get(
            f"/api/v1/agent/admin/profiles/{profile_id}",
            headers=admin_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["profile_id"] == str(profile_id)

        updated = client.patch(
            f"/api/v1/agent/admin/profiles/{profile_id}",
            headers=admin_headers,
            json={"display_name": "renamed", "config": '{"model": "compact"}'},
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "renamed"
        assert updated.json()["config"] == '{"model": "compact"}'

        deleted = client.delete(
            f"/api/v1/agent/admin/profiles/{profile_id}",
            headers=admin_headers,
        )
        assert deleted.status_code == 204

        gone = client.get(
            f"/api/v1/agent/admin/profiles/{profile_id}",
            headers=admin_headers,
        )
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "profile_not_found"

    def test_patch_and_get_unknown_profile_return_404(self, client, admin_headers) -> None:
        unknown_id = uuid4()
        assert (
            client.get(
                f"/api/v1/agent/admin/profiles/{unknown_id}",
                headers=admin_headers,
            ).status_code
            == 404
        )
        patched = client.patch(
            f"/api/v1/agent/admin/profiles/{unknown_id}",
            headers=admin_headers,
            json={"display_name": "ghost"},
        )
        assert patched.status_code == 404
        assert patched.json()["error"]["code"] == "profile_not_found"


class TestBindings:
    def test_create_binding_validates_term_exists(self, client, admin_headers) -> None:
        profile = _create_profile(client, admin_headers)
        missing = client.post(
            "/api/v1/agent/admin/bindings",
            headers=admin_headers,
            json={
                "profile_id": profile["profile_id"],
                "term_id": str(uuid4()),
            },
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "instance_not_found"

    def test_create_list_get_status_transitions_delete(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        profile = _create_profile(client, admin_headers)
        term = provision_term(name="binding-term")
        binding = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        binding_id = UUID(str(binding["binding_id"]))
        assert binding["status"] == "pending"
        assert binding["runtime_ref"] is None

        listed = client.get("/api/v1/agent/admin/bindings", headers=admin_headers)
        assert listed.status_code == 200
        assert any(item["binding_id"] == str(binding_id) for item in listed.json()["bindings"])

        detail = client.get(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["profile_id"] == profile["profile_id"]
        assert detail.json()["term_id"] == str(term.instance_id)

        # pending -> ready
        ready = client.patch(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
            json={"status": "ready"},
        )
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        # ready -> revoked (terminal state)
        revoked = client.patch(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
            json={"status": "revoked"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"

        deleted = client.delete(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
        )
        assert deleted.status_code == 204
        gone = client.get(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
        )
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "binding_not_found"

    def test_binding_revoked_or_disabled_revokes_pending_approvals(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        """Spec §5: a revoked/disabled binding cannot keep approvals alive."""
        from datetime import UTC, datetime, timedelta

        from termflow_control_plane.plugins.agent_broker.agent.permissions import (
            ApprovalState,
            canonical_hash,
        )

        profile = _create_profile(client, admin_headers)
        term = provision_term(name="revoke-binding-term")
        binding = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        binding_id = UUID(str(binding["binding_id"]))
        repositories: RepositoryBundle = client.app.state.repositories

        async def _seed_approvals() -> list[UUID]:
            conversation = await repositories.agent_conversations.create(
                binding_id=binding_id, title="revoke-approvals"
            )
            created = []
            for index in range(2):
                approval = await repositories.approvals.create(
                    binding_id=binding_id,
                    conversation_id=conversation.id,
                    tool_call_id=f"tool-{index}",
                    canonical_hash=canonical_hash(
                        __import__(
                            "termflow_control_plane.plugins.agent_broker.agent.permissions",
                            fromlist=["ApprovalArgsHashInput"],
                        ).ApprovalArgsHashInput(
                            schema_version=1,
                            operation="send_text",
                            instance_id=term.instance_id,
                            pane_id="%1",
                            pane_incarnation="1",
                            encoded_bytes=b"x",
                            submit=False,
                            cursor_precondition=None,
                            run_id=None,
                            grant_id=None,
                            expiry=datetime.now(UTC) + timedelta(minutes=5),
                            policy_epoch=1,
                        )
                    ),
                    auth_epoch=1,
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
                created.append(approval.id)
            return created

        approval_ids = client.portal.call(_seed_approvals)

        for status in ("revoked", "disabled"):
            patched = client.patch(
                f"/api/v1/agent/admin/bindings/{binding_id}",
                headers=admin_headers,
                json={"status": status},
            )
            assert patched.status_code == 200
            assert patched.json()["status"] == status

        for approval_id in approval_ids:
            detail = client.get(
                f"/api/v1/agent/approvals/{approval_id}",
                headers=admin_headers,
            )
            assert detail.status_code == 200
            assert detail.json()["state"] == ApprovalState.REVOKED.value

    def test_binding_runtime_update_requires_all_runtime_fields(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        profile = _create_profile(client, admin_headers)
        term = provision_term(name="runtime-binding-term")
        binding = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        binding_id = UUID(str(binding["binding_id"]))

        partial = client.patch(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
            json={"runtime_ref": "runtime-1"},
        )
        assert partial.status_code == 422
        assert partial.json()["error"]["code"] == "invalid_runtime_update"

        complete = client.patch(
            f"/api/v1/agent/admin/bindings/{binding_id}",
            headers=admin_headers,
            json={
                "runtime_ref": "runtime-1",
                "runtime_epoch": 7,
                "capability_ref": "cap-1",
            },
        )
        assert complete.status_code == 200
        assert complete.json()["runtime_ref"] == "runtime-1"
        assert complete.json()["runtime_epoch"] == 7
        assert complete.json()["capability_ref"] == "cap-1"

    def test_duplicate_active_binding_is_rejected(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        profile = _create_profile(client, admin_headers)
        term = provision_term(name="duplicate-binding-term")
        _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        duplicate = client.post(
            "/api/v1/agent/admin/bindings",
            headers=admin_headers,
            json={
                "profile_id": profile["profile_id"],
                "term_id": str(term.instance_id),
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "binding_already_exists"


class TestTokens:
    def test_create_returns_raw_token_once_and_stores_only_hash(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        profile = _create_profile(client, admin_headers)
        term = provision_term(name="token-binding-term")
        binding = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        binding_id = UUID(str(binding["binding_id"]))
        expires_at = datetime.now(UTC) + timedelta(hours=2)

        created = _create_token(
            client,
            admin_headers,
            binding_id=binding_id,
            scopes=["observe", "tools.read"],
            expires_at=expires_at,
        )
        raw_token = created["raw_token"]
        assert raw_token
        assert created["binding_id"] == str(binding_id)
        assert sorted(created["scopes"]) == ["observe", "tools.read"]
        token_id = UUID(str(created["token_id"]))

        repositories: RepositoryBundle = client.app.state.repositories
        stored = client.portal.call(
            repositories.agent_tokens.list_for_binding,
            binding_id,
        )
        assert len(stored) == 1
        assert stored[0].token_hash == hash_token(raw_token)
        # The raw token is never persisted.
        assert stored[0].token_hash != raw_token
        assert stored[0].id == token_id
        by_hash = client.portal.call(
            repositories.agent_tokens.get_by_hash,
            hash_token(raw_token),
        )
        assert by_hash is not None and by_hash.id == token_id
        assert sorted(decode_scopes(stored[0].scopes)) == ["observe", "tools.read"]

    def test_token_list_filters_by_binding(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        profile = _create_profile(client, admin_headers)
        first_term = provision_term(name="token-list-term-a")
        second_term = provision_term(name="token-list-term-b")
        first = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=first_term.instance_id,
        )
        second = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=second_term.instance_id,
        )
        _create_token(
            client,
            admin_headers,
            binding_id=UUID(str(first["binding_id"])),
            scopes=["observe"],
        )
        _create_token(
            client,
            admin_headers,
            binding_id=UUID(str(second["binding_id"])),
            scopes=["observe"],
        )

        first_list = client.get(
            f"/api/v1/agent/admin/tokens?binding_id={first['binding_id']}",
            headers=admin_headers,
        )
        assert first_list.status_code == 200
        tokens = first_list.json()["tokens"]
        assert len(tokens) == 1
        assert tokens[0]["binding_id"] == first["binding_id"]
        assert tokens[0]["revoked_at"] is None

    def test_token_revoke(self, client, admin_headers, provision_term) -> None:
        profile = _create_profile(client, admin_headers)
        term = provision_term(name="revoke-binding-term")
        binding = _create_binding(
            client,
            admin_headers,
            profile_id=UUID(str(profile["profile_id"])),
            term_id=term.instance_id,
        )
        binding_id = UUID(str(binding["binding_id"]))
        created = _create_token(client, admin_headers, binding_id=binding_id)
        token_id = UUID(str(created["token_id"]))

        revoked = client.post(
            f"/api/v1/agent/admin/tokens/{token_id}/revoke",
            headers=admin_headers,
        )
        assert revoked.status_code == 200

        repositories: RepositoryBundle = client.app.state.repositories
        stored = client.portal.call(
            repositories.agent_tokens.list_for_binding,
            binding_id,
        )
        assert stored[0].id == token_id
        assert stored[0].revoked_at is not None

        listed = client.get(
            f"/api/v1/agent/admin/tokens?binding_id={binding_id}",
            headers=admin_headers,
        )
        assert listed.json()["tokens"][0]["revoked_at"] is not None

    def test_token_revoke_unknown_returns_404(self, client, admin_headers) -> None:
        missing = client.post(
            f"/api/v1/agent/admin/tokens/{uuid4()}/revoke",
            headers=admin_headers,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "token_not_found"

    def test_token_create_for_unknown_binding_returns_404(self, client, admin_headers) -> None:
        missing = client.post(
            "/api/v1/agent/admin/tokens",
            headers=admin_headers,
            json={
                "binding_id": str(uuid4()),
                "scopes": ["observe"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "binding_not_found"


class TestAdminAuth:
    def test_unauthenticated_is_rejected(self, client) -> None:
        assert client.get("/api/v1/agent/admin/profiles").status_code == 401
        assert client.post("/api/v1/agent/admin/profiles", json={}).status_code == 401
        assert client.get("/api/v1/agent/admin/bindings").status_code == 401
        assert client.get("/api/v1/agent/admin/tokens").status_code == 401

    def test_non_admin_bearer_is_forbidden(self, client) -> None:
        issued = client.post(
            "/api/v1/admin/cli-tokens",
            json={"admin_token": ADMIN_TOKEN, "scopes": ["computers.read"]},
        )
        assert issued.status_code == 201, issued.text
        headers = {"Authorization": f"Bearer {issued.json()['access_token']}"}

        denied = client.post(
            "/api/v1/agent/admin/profiles",
            headers=headers,
            json={"display_name": "nope", "backend_kind": "opencode"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "insufficient_scope"

    def test_admin_bearer_succeeds(self, client, admin_headers) -> None:
        assert (
            client.get("/api/v1/agent/admin/profiles", headers=admin_headers).status_code == 200
        )
