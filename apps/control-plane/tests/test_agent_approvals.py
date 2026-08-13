"""Approval Request domain and API tests (plan §12.1, task M5.1).

Covers the approval workflow implemented by
:mod:`termflow_control_plane.plugins.agent_broker.agent.permissions`:

- ``create_approval`` creates a pending single-use request.
- ``decide`` is an atomic CAS from ``pending`` to ``approved``/``denied``
  bound to the request's auth epoch: stale epochs, expired, revoked, and
  already-decided requests are rejected and a double decision can never win.
- ``consume``/``mark_unknown``/``revoke``/``expire_pending``/
  ``revoke_for_binding`` implement the remaining plan §12.1 lifecycle.
- ``canonical_hash`` is deterministic and sensitive to every input field
  (schema version, operation, instance/pane incarnation, encoded text/key
  bytes, submit flag, cursor precondition, run/grant id, expiry, policy
  epoch), locked by an explicit test vector.

The API tests exercise the endpoints under ``/api/v1/agent/approvals``:
detail, decide (409 double / 410 expired / 404 unknown), revoke, and
conversation-scoped listing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import (
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalAlreadyConsumed,
    ApprovalAlreadyDecided,
    ApprovalArgsHashInput,
    ApprovalAuthEpochStale,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalPolicy,
    ApprovalRevoked,
    ApprovalState,
    ApprovalToolCallConflict,
    canonical_hash,
)
from termflow_protocol.agent import ApprovalDecision

#: Fixed canonical-encoding vector: locks the hash so an encoding change
#: (which would invalidate every in-flight approval) fails loudly.
FIXED_HASH_VECTOR = "99fefde4bddf81ea1ffee041e888912ab9a91ff66dbd9ded6d1aef2fde1a4af2"


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'approvals.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def policy(repositories: RepositoryBundle) -> ApprovalPolicy:
    return ApprovalPolicy(
        repositories,
        repositories.session_factory,  # type: ignore[attr-defined]
    )


async def _seed_conversation(repos: RepositoryBundle) -> tuple[UUID, UUID]:
    """Create a profile, term, binding, and conversation; return (binding_id, conversation_id)."""
    profile = await repos.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repos.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repos.instances.register_or_rotate(
        uuid4(),
        installation.id,
        f"term-{uuid4().hex[:8]}",
        digest_secret(f"instance-{uuid4().hex}"),
    )
    binding = await repos.agent_bindings.create(profile_id=profile.id, term_id=term.id)
    conversation = await repos.agent_conversations.create(
        binding_id=binding.id,
        title="approval-test",
    )
    return binding.id, conversation.id


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


async def _create_pending(
    policy: ApprovalPolicy,
    repos: RepositoryBundle,
    *,
    auth_epoch: int = 1,
    **overrides: object,
):
    binding_id, conversation_id = await _seed_conversation(repos)
    return binding_id, conversation_id, await policy.create_approval(
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id=f"tool-{uuid4().hex[:12]}",
        canonical_hash=canonical_hash(_hash_input()),
        auth_epoch=auth_epoch,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        **overrides,
    )


class TestCanonicalHash:
    def test_same_input_produces_same_hash(self) -> None:
        args = _hash_input()
        assert canonical_hash(args) == canonical_hash(args)

    def test_deterministic_across_calls_and_fixed_vector(self) -> None:
        args = ApprovalArgsHashInput(
            schema_version=1,
            operation="key",
            instance_id=UUID("11111111-1111-1111-1111-111111111111"),
            pane_id="p1",
            pane_incarnation="incarnation-1",
            encoded_bytes=b"\x1b[A",
            submit=False,
            cursor_precondition="cursor-42",
            run_id=UUID("22222222-2222-2222-2222-222222222222"),
            grant_id=None,
            expiry=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
            policy_epoch=1,
        )
        first = canonical_hash(args)
        second = canonical_hash(args)
        assert first == second
        assert first == FIXED_HASH_VECTOR
        assert len(first) == 64
        assert all(character in "0123456789abcdef" for character in first)

    @pytest.mark.parametrize(
        ("field", "changed"),
        [
            ("schema_version", 2),
            ("operation", "text"),
            ("instance_id", uuid4()),
            ("pane_id", "p2"),
            ("pane_incarnation", "incarnation-2"),
            ("encoded_bytes", b"ls -la\n"),
            ("submit", True),
            ("cursor_precondition", "cursor-99"),
            ("cursor_precondition", None),
            ("run_id", uuid4()),
            ("run_id", None),
            ("grant_id", "grant-1"),
            ("expiry", datetime.now(UTC) + timedelta(hours=1)),
            ("policy_epoch", 2),
        ],
    )
    def test_any_field_change_changes_the_hash(self, field: str, changed: object) -> None:
        base = _hash_input()
        assert canonical_hash(base) != canonical_hash(_hash_input(**{field: changed}))

    def test_naive_expiry_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_hash(_hash_input(expiry=datetime(2030, 1, 1)))


class TestApprovalPolicy:
    async def test_create_makes_pending_request(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        binding_id, conversation_id, approval = await _create_pending(policy, repositories)
        assert approval.binding_id == binding_id
        assert approval.conversation_id == conversation_id
        assert approval.state == ApprovalState.PENDING
        assert approval.decided_at is None
        assert approval.decision is None
        assert approval.auth_epoch == 1
        assert len(approval.canonical_hash) == 64

    async def test_decide_approve(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        state = await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="test-admin",
            auth_epoch=1,
        )
        assert state is ApprovalState.APPROVED
        decided = await repositories.approvals.get_by_id(approval.id)
        assert decided is not None
        assert decided.state == ApprovalState.APPROVED
        assert decided.decision == ApprovalDecision.APPROVED
        assert decided.decided_at is not None

    async def test_decide_deny(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        state = await policy.decide(
            approval.id,
            decision=ApprovalDecision.DENIED,
            actor="test-admin",
            auth_epoch=1,
        )
        assert state is ApprovalState.DENIED
        decided = await repositories.approvals.get_by_id(approval.id)
        assert decided is not None
        assert decided.state == ApprovalState.DENIED
        assert decided.decision == ApprovalDecision.DENIED

    async def test_double_decision_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="test-admin",
            auth_epoch=1,
        )
        with pytest.raises(ApprovalAlreadyDecided):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.DENIED,
                actor="test-admin",
                auth_epoch=1,
            )
        decided = await repositories.approvals.get_by_id(approval.id)
        assert decided is not None
        assert decided.state == ApprovalState.APPROVED
        assert decided.decision == ApprovalDecision.APPROVED

    async def test_decide_on_expired_request_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        expiry = datetime.now(UTC) + timedelta(minutes=5)
        with pytest.raises(ApprovalExpired):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=1,
                now=expiry + timedelta(seconds=1),
            )
        swept = await repositories.approvals.get_by_id(approval.id)
        assert swept is not None
        assert swept.state == ApprovalState.PENDING

    async def test_decide_on_swept_request_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.expire_pending(now=datetime.now(UTC) + timedelta(hours=1))
        with pytest.raises(ApprovalExpired):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=1,
            )

    async def test_decide_on_revoked_request_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.revoke(approval.id, actor="test-admin")
        with pytest.raises(ApprovalRevoked):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=1,
            )

    @pytest.mark.parametrize(
        ("created_epoch", "deciding_epoch"),
        [(2, 1), (1, 2)],
    )
    async def test_decide_from_stale_auth_epoch_is_rejected(
        self,
        policy: ApprovalPolicy,
        repositories: RepositoryBundle,
        created_epoch: int,
        deciding_epoch: int,
    ) -> None:
        _, _, approval = await _create_pending(
            policy, repositories, auth_epoch=created_epoch
        )
        with pytest.raises(ApprovalAuthEpochStale):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=deciding_epoch,
            )
        unchanged = await repositories.approvals.get_by_id(approval.id)
        assert unchanged is not None
        assert unchanged.state == ApprovalState.PENDING

    async def test_decide_unknown_approval_raises_not_found(
        self, policy: ApprovalPolicy
    ) -> None:
        with pytest.raises(ApprovalNotFound):
            await policy.decide(
                uuid4(),
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=1,
            )

    async def test_consume_once_and_reject_second(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="test-admin",
            auth_epoch=1,
        )
        state = await policy.consume(approval.id)
        assert state is ApprovalState.CONSUMED
        with pytest.raises(ApprovalAlreadyConsumed):
            await policy.consume(approval.id)
        consumed = await repositories.approvals.get_by_id(approval.id)
        assert consumed is not None
        assert consumed.state == ApprovalState.CONSUMED

    async def test_consume_pending_request(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        state = await policy.consume(approval.id)
        assert state is ApprovalState.CONSUMED

    async def test_consume_denied_request_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.DENIED,
            actor="test-admin",
            auth_epoch=1,
        )
        with pytest.raises(ApprovalAlreadyDecided):
            await policy.consume(approval.id)

    async def test_mark_unknown_is_terminal_and_never_replayable(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="test-admin",
            auth_epoch=1,
        )
        state = await policy.mark_unknown(approval.id)
        assert state is ApprovalState.UNKNOWN
        unknown = await repositories.approvals.get_by_id(approval.id)
        assert unknown is not None
        assert unknown.state == ApprovalState.UNKNOWN
        assert unknown.decision == "unknown"
        # Uncertain outcomes can never be replayed automatically: neither a
        # second decision nor a consume can move the request again.
        with pytest.raises(ApprovalAlreadyDecided):
            await policy.decide(
                approval.id,
                decision=ApprovalDecision.APPROVED,
                actor="test-admin",
                auth_epoch=1,
            )
        with pytest.raises(ApprovalAlreadyDecided):
            await policy.consume(approval.id)

    async def test_revoke_pending_and_approved(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, pending = await _create_pending(policy, repositories)
        assert await policy.revoke(pending.id, actor="test-admin") is ApprovalState.REVOKED
        revoked_pending = await repositories.approvals.get_by_id(pending.id)
        assert revoked_pending is not None
        assert revoked_pending.state == ApprovalState.REVOKED
        assert revoked_pending.decision == "revoked"

        _, _, approved = await _create_pending(policy, repositories)
        await policy.decide(
            approved.id,
            decision=ApprovalDecision.APPROVED,
            actor="test-admin",
            auth_epoch=1,
        )
        assert await policy.revoke(approved.id, actor="test-admin") is ApprovalState.REVOKED

    async def test_second_revoke_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        _, _, approval = await _create_pending(policy, repositories)
        await policy.revoke(approval.id, actor="test-admin")
        with pytest.raises(ApprovalRevoked):
            await policy.revoke(approval.id, actor="test-admin")

    async def test_revoke_for_binding_revokes_only_that_binding(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        binding_id, conversation_id = await _seed_conversation(repositories)
        other_binding_id, other_conversation_id = await _seed_conversation(repositories)
        first = await policy.create_approval(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        second = await policy.create_approval(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        unrelated = await policy.create_approval(
            binding_id=other_binding_id,
            conversation_id=other_conversation_id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        count = await policy.revoke_for_binding(binding_id)
        assert count == 2
        for approval_id in (first.id, second.id):
            revoked = await repositories.approvals.get_by_id(approval_id)
            assert revoked is not None
            assert revoked.state == ApprovalState.REVOKED
        untouched = await repositories.approvals.get_by_id(unrelated.id)
        assert untouched is not None
        assert untouched.state == ApprovalState.PENDING

    async def test_expire_pending_sweeps_only_past_expiry(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        binding_id, conversation_id = await _seed_conversation(repositories)
        expired = await policy.create_approval(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        live = await policy.create_approval(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        count = await policy.expire_pending(now=datetime.now(UTC) + timedelta(minutes=5))
        assert count == 1
        swept = await repositories.approvals.get_by_id(expired.id)
        assert swept is not None
        assert swept.state == ApprovalState.EXPIRED
        assert swept.decision == "expired"
        untouched = await repositories.approvals.get_by_id(live.id)
        assert untouched is not None
        assert untouched.state == ApprovalState.PENDING

    async def test_create_validation(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        binding_id, conversation_id = await _seed_conversation(repositories)
        with pytest.raises(ValueError):
            await policy.create_approval(
                binding_id=binding_id,
                conversation_id=conversation_id,
                tool_call_id="tool-1",
                canonical_hash="not-a-hash",
                auth_epoch=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        with pytest.raises(ValueError):
            await policy.create_approval(
                binding_id=binding_id,
                conversation_id=conversation_id,
                tool_call_id="tool-1",
                canonical_hash=canonical_hash(_hash_input()),
                auth_epoch=1,
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        with pytest.raises(ValueError):
            await policy.create_approval(
                binding_id=binding_id,
                conversation_id=conversation_id,
                tool_call_id="",
                canonical_hash=canonical_hash(_hash_input()),
                auth_epoch=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

    async def test_duplicate_tool_call_in_conversation_is_rejected(
        self, policy: ApprovalPolicy, repositories: RepositoryBundle
    ) -> None:
        binding_id, conversation_id = await _seed_conversation(repositories)
        kwargs = dict(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id="same-tool-call",
            canonical_hash=canonical_hash(_hash_input()),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        await policy.create_approval(**kwargs)
        with pytest.raises(ApprovalToolCallConflict):
            await policy.create_approval(**kwargs)


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


def _create_profile(client: TestClient, admin_headers: dict[str, str]) -> dict[str, object]:
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
    term = provision_term(name="approval-term")
    binding = _create_binding(
        client,
        admin_headers,
        profile_id=UUID(str(profile["profile_id"])),
        term_id=term.instance_id,
    )
    return UUID(str(binding["binding_id"]))


class TestApprovalsApi:
    def _seed_approval(
        self,
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

    def test_get_approval_detail(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=conversation_id
        )

        detail = client.get(f"/api/v1/agent/approvals/{approval_id}", headers=admin_headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["approval_id"] == str(approval_id)
        assert body["conversation_id"] == str(conversation_id)
        assert body["tool_call_id"] == "tool-call-1"
        assert len(body["canonical_hash"]) == 64
        assert body["state"] == "pending"
        assert body["decision"] is None
        assert body["decided_at"] is None
        assert body["auth_epoch"] == 1
        assert body["binding"]["binding_id"] == str(binding_id)
        assert body["binding"]["term_id"] is not None

    def test_get_approval_detail_exposes_m52_display_metadata(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        repositories: RepositoryBundle = client.app.state.repositories

        async def _create() -> UUID:
            approval = await repositories.approvals.create(
                binding_id=binding_id,
                conversation_id=conversation_id,
                tool_call_id="tool-call-m52",
                canonical_hash=canonical_hash(_hash_input()),
                auth_epoch=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                pane_id="%1",
                operation="send_text",
                intent_summary="run the test suite",
            )
            return approval.id

        approval_id = client.portal.call(_create)

        detail = client.get(f"/api/v1/agent/approvals/{approval_id}", headers=admin_headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["pane_id"] == "%1"
        assert body["operation"] == "send_text"
        assert body["intent_summary"] == "run the test suite"

    def test_approval_detail_legacy_rows_degrade_to_none(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=UUID(str(conversation["conversation_id"]))
        )
        detail = client.get(f"/api/v1/agent/approvals/{approval_id}", headers=admin_headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["pane_id"] is None
        assert body["operation"] is None
        assert body["intent_summary"] is None

    def test_decide_approve_and_deny_via_api(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=conversation_id
        )

        approved = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert approved.status_code == 200
        assert approved.json()["state"] == "approved"
        assert approved.json()["decision"] == "approved"

        denied_id = self._seed_approval(
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

    def test_double_decision_returns_409(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=conversation_id
        )

        first = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "deny"},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "approval_already_decided"

    def test_decide_expired_returns_410(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=conversation_id
        )
        repositories: RepositoryBundle = client.app.state.repositories
        client.portal.call(
            lambda: repositories.approvals.expire_pending(
                now=datetime.now(UTC) + timedelta(hours=1)
            )
        )

        response = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "approval_expired"

    def test_decide_unknown_approval_returns_404(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/agent/approvals/{uuid4()}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "approval_not_found"

    def test_stale_auth_epoch_decision_returns_409(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client,
            binding_id=binding_id,
            conversation_id=conversation_id,
            auth_epoch=1,
        )
        repositories: RepositoryBundle = client.app.state.repositories
        client.portal.call(repositories.auth_state.reset_and_increment_epoch)

        response = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "approval_auth_epoch_stale"

    def test_revoke_via_api_and_decide_afterwards_returns_409(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        approval_id = self._seed_approval(
            client, binding_id=binding_id, conversation_id=conversation_id
        )

        revoked = client.post(
            f"/api/v1/agent/approvals/{approval_id}/revoke",
            headers=admin_headers,
        )
        assert revoked.status_code == 200
        assert revoked.json()["state"] == "revoked"
        assert revoked.json()["decision"] == "revoked"

        decided = client.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            headers=admin_headers,
            json={"decision": "approve"},
        )
        assert decided.status_code == 409
        assert decided.json()["error"]["code"] == "approval_revoked"

    def test_list_by_conversation_and_without_filter(
        self, client: TestClient, admin_headers: dict[str, str], provision_term
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
        approval_one = self._seed_approval(
            client, binding_id=binding_id, conversation_id=first_id
        )
        approval_two = self._seed_approval(
            client,
            binding_id=binding_id,
            conversation_id=first_id,
            tool_call_id="tool-call-2",
        )
        self._seed_approval(
            client,
            binding_id=binding_id,
            conversation_id=second_id,
            tool_call_id="tool-call-3",
        )

        listed = client.get(
            f"/api/v1/agent/approvals?conversation_id={first_id}",
            headers=admin_headers,
        )
        assert listed.status_code == 200
        approval_ids = [item["approval_id"] for item in listed.json()["approvals"]]
        assert set(approval_ids) == {str(approval_one), str(approval_two)}
        assert all(item["state"] == "pending" for item in listed.json()["approvals"])

        all_approvals = client.get("/api/v1/agent/approvals", headers=admin_headers)
        assert all_approvals.status_code == 200
        assert len(all_approvals.json()["approvals"]) == 3

    def test_list_unknown_conversation_returns_404(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = client.get(
            f"/api/v1/agent/approvals?conversation_id={uuid4()}",
            headers=admin_headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"

    def test_approvals_require_authentication(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/agent/approvals/{uuid4()}")
        assert response.status_code == 401
