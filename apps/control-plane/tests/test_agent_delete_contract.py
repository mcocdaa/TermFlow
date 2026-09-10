"""Delete-contract tests for Agent conversations, bindings, and profiles.

Plan §15/§17: deletion is routed through the durable tombstone/cleanup path
- active runs are cancelled, a cleanup tombstone is created BEFORE the row
deletion, and the backend (OpenCode) session is proven deleted before any
B-side row goes away.  When the backend deletion cannot be proven the rows
stay and the endpoint fails with ``deletion_pending`` while the tombstone
keeps retrying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from termflow_control_plane.persistence.models import AgentCleanupJob
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendOperationResult,
    BackendOutcome,
)
from termflow_control_plane.plugins.agent_broker.plugin import (
    build_agent_cleanup_handlers,
    run_agent_recovery,
)


class FakeAdapter:
    """Adapter stub whose delete_conversation outcome is scriptable."""

    def __init__(
        self,
        *,
        outcome: BackendOutcome = BackendOutcome.CONFIRMED,
        message: str = "deleted",
    ) -> None:
        self._outcome = outcome
        self._message = message
        self.deleted_refs: list[BackendConversationRef] = []

    async def delete_conversation(
        self, ref: BackendConversationRef
    ) -> BackendOperationResult:
        self.deleted_refs.append(ref)
        return BackendOperationResult(outcome=self._outcome, message=self._message)


class FakePipeline:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter


class FakeRegistry:
    """Registry stub mapping binding ids onto fake pipelines."""

    def __init__(self, pipelines: dict[UUID, FakePipeline] | None = None) -> None:
        self._pipelines = pipelines or {}

    def pipeline_for(self, binding_id: UUID) -> FakePipeline | None:
        return self._pipelines.get(binding_id)


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
    *,
    name: str = "delete-term",
) -> tuple[UUID, UUID]:
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
    term = provision_term(name=name)
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": profile.json()["profile_id"], "term_id": str(term.instance_id)},
    )
    assert binding.status_code == 201, binding.text
    return UUID(str(profile.json()["profile_id"])), UUID(
        str(binding.json()["binding_id"])
    )


def _create_conversation(
    client: TestClient, admin_headers: dict[str, str], binding_id: UUID
) -> UUID:
    response = client.post(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        json={"binding_id": str(binding_id)},
    )
    assert response.status_code == 201, response.text
    return UUID(str(response.json()["conversation_id"]))


async def _seed_backend_ref(
    client: TestClient, conversation_id: UUID, provider_ref: str
) -> None:
    repositories: RepositoryBundle = client.app.state.repositories
    await repositories.agent_backend_conversations.create(
        conversation_id=conversation_id,
        backend_kind="opencode",
        backend_version="0.3.0",
        runtime_id="runtime-1",
        binding_capability_epoch=1,
        provider_ref=provider_ref,
    )


async def _seed_active_run(client: TestClient, conversation_id: UUID) -> UUID:
    repositories: RepositoryBundle = client.app.state.repositories
    run = await repositories.agent_runs.create(
        conversation_id=conversation_id,
        run_state="running",
        started_at=datetime.now(UTC),
    )
    return run.id


async def _cleanup_job(
    client: TestClient, *, target_kind: str, target_ref: str
) -> AgentCleanupJob | None:
    """The tombstone row itself (list_pending hides backed-off pending jobs)."""
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        return await session.scalar(
            select(AgentCleanupJob).where(
                AgentCleanupJob.target_kind == target_kind,
                AgentCleanupJob.target_ref == target_ref,
            )
        )


# ---------------------------------------------------------------------------
# Conversation deletion
# ---------------------------------------------------------------------------


def test_delete_conversation_cancels_runs_and_waits_for_provider_retention_receipt(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter()
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        await _seed_backend_ref(client, conversation_id, "sess_abc123")
        await _seed_active_run(client, conversation_id)

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        # Rows are gone; the backend adapter was asked to delete the session.
        assert await repositories.agent_conversations.get_by_id(conversation_id) is None
        assert [
            ref.provider_ref for ref in adapter.deleted_refs
        ] == ["sess_abc123"]
        # The provider-owned retention receipt still requires independent
        # confirmation even though B proved the backend session deletion.
        pending = await repositories.cleanup_jobs.list_pending()
        assert len(pending) == 1
        receipts = await repositories.cleanup_jobs.list_receipts(pending[0].id)
        assert [
            row.artifact_kind
            for row in receipts
            if row.state == "pending"
        ] == ["provider_retention"]

    client.portal.call(verify)


def test_delete_conversation_without_backend_session_is_plain_success(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter()
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    deleted = client.delete(
        f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers
    )
    assert deleted.status_code == 204, deleted.text

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_conversations.get_by_id(conversation_id) is None
        # No backend session existed: the adapter was never asked.
        assert adapter.deleted_refs == []

    client.portal.call(verify)


def test_delete_conversation_backend_unconfirmed_keeps_rows_and_marks_pending(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter(outcome=BackendOutcome.UNKNOWN, message="backend down")
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        run_id = await _seed_active_run(client, conversation_id)
        await _seed_backend_ref(client, conversation_id, "sess_xyz")
        # Capture for the verification below.
        client.app.state.seeded_run_id = run_id

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        # The rows stay so the tombstone retry can still prove the deletion.
        assert await repositories.agent_conversations.get_by_id(conversation_id) is not None
        # The active run was cancelled before any deletion.
        run = await repositories.agent_runs.get_by_id(client.app.state.seeded_run_id)
        assert run is not None and run.run_state == "cancelled"
        job = await _cleanup_job(
            client, target_kind="conversation", target_ref=str(conversation_id)
        )
        assert job is not None
        assert job.state == "pending"
        assert job.attempt_count == 1
        assert "backend down" in (job.last_error or "")

    client.portal.call(verify)


def test_delete_conversation_without_pipeline_fails_closed(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    # No pipeline mapped for the binding: the backend deletion is unprovable.
    client.app.state.agent_runtime_registry = FakeRegistry({})

    async def seed() -> None:
        await _seed_backend_ref(client, conversation_id, "sess_orphan")

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_conversations.get_by_id(conversation_id) is not None
        job = await _cleanup_job(
            client, target_kind="conversation", target_ref=str(conversation_id)
        )
        assert job is not None and job.state == "pending"
        assert "no mapped runtime pipeline" in (job.last_error or "")

    client.portal.call(verify)


def test_delete_missing_conversation_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    from uuid import uuid4

    deleted = client.delete(
        f"/api/v1/agent/conversations/{uuid4()}", headers=admin_headers
    )
    assert deleted.status_code == 404
    assert deleted.json()["error"]["code"] == "conversation_not_found"


# ---------------------------------------------------------------------------
# Binding deletion
# ---------------------------------------------------------------------------


def test_delete_binding_cancels_runs_and_waits_for_provider_retention_receipts(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    first = _create_conversation(client, admin_headers, binding_id)
    second = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter()
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        await _seed_backend_ref(client, first, "sess_1")
        await _seed_backend_ref(client, second, "sess_2")
        await _seed_active_run(client, first)

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/admin/bindings/{binding_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_bindings.get_by_id(binding_id) is None
        assert await repositories.agent_conversations.get_by_id(first) is None
        assert await repositories.agent_conversations.get_by_id(second) is None
        assert {ref.provider_ref for ref in adapter.deleted_refs} == {
            "sess_1",
            "sess_2",
        }
        pending = await repositories.cleanup_jobs.list_pending()
        assert len(pending) == 1
        receipts = await repositories.cleanup_jobs.list_receipts(pending[0].id)
        assert {
            row.artifact_ref for row in receipts if row.artifact_kind == "provider_retention"
        } == {"provider:sess_1", "provider:sess_2"}

    client.portal.call(verify)


def test_delete_binding_backend_unconfirmed_keeps_rows_and_marks_pending(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter(outcome=BackendOutcome.UNKNOWN, message="backend down")
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        await _seed_backend_ref(client, conversation_id, "sess_b")

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/admin/bindings/{binding_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_bindings.get_by_id(binding_id) is not None
        job = await _cleanup_job(
            client, target_kind="binding", target_ref=str(binding_id)
        )
        assert job is not None and job.state == "pending"

    client.portal.call(verify)


def test_delete_missing_binding_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    from uuid import uuid4

    deleted = client.delete(
        f"/api/v1/agent/admin/bindings/{uuid4()}", headers=admin_headers
    )
    assert deleted.status_code == 404
    assert deleted.json()["error"]["code"] == "binding_not_found"


# ---------------------------------------------------------------------------
# Profile deletion
# ---------------------------------------------------------------------------


def test_delete_profile_sweeps_sessions_and_waits_for_provider_retention_receipt(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    profile_id, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter()
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        await _seed_backend_ref(client, conversation_id, "sess_p")
        await _seed_active_run(client, conversation_id)

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/admin/profiles/{profile_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_profiles.get_by_id(profile_id) is None
        assert await repositories.agent_bindings.get_by_id(binding_id) is None
        assert await repositories.agent_conversations.get_by_id(conversation_id) is None
        assert [ref.provider_ref for ref in adapter.deleted_refs] == ["sess_p"]
        pending = await repositories.cleanup_jobs.list_pending()
        assert len(pending) == 1
        receipts = await repositories.cleanup_jobs.list_receipts(pending[0].id)
        assert [
            row.artifact_ref
            for row in receipts
            if row.artifact_kind == "provider_retention"
        ] == ["provider:sess_p"]

    client.portal.call(verify)


def test_delete_profile_backend_unconfirmed_keeps_rows_and_marks_pending(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    profile_id, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter(outcome=BackendOutcome.UNKNOWN, message="backend down")
    client.app.state.agent_runtime_registry = FakeRegistry(
        {binding_id: FakePipeline(adapter)}
    )

    async def seed() -> None:
        await _seed_backend_ref(client, conversation_id, "sess_p2")

    client.portal.call(seed)

    deleted = client.delete(
        f"/api/v1/agent/admin/profiles/{profile_id}", headers=admin_headers
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["state"] == "deletion_pending"

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_profiles.get_by_id(profile_id) is not None
        assert await repositories.agent_bindings.get_by_id(binding_id) is not None
        job = await _cleanup_job(
            client, target_kind="profile", target_ref=str(profile_id)
        )
        assert job is not None and job.state == "pending"

    client.portal.call(verify)


def test_delete_missing_profile_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    from uuid import uuid4

    deleted = client.delete(
        f"/api/v1/agent/admin/profiles/{uuid4()}", headers=admin_headers
    )
    assert deleted.status_code == 404
    assert deleted.json()["error"]["code"] == "profile_not_found"


# ---------------------------------------------------------------------------
# Tombstone retry completes a deferred deletion
# ---------------------------------------------------------------------------


def test_recovery_retry_completes_deferred_conversation_deletion(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    """A crash between tombstone creation and row deletion leaves the
    conversation intact; the recovery sweep with the registered handlers
    proves the backend deletion and finishes the durable work."""
    _, binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation_id = _create_conversation(client, admin_headers, binding_id)
    adapter = FakeAdapter()
    fake_registry = FakeRegistry({binding_id: FakePipeline(adapter)})

    async def seed() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        await _seed_backend_ref(client, conversation_id, "sess_retry")
        await repositories.cleanup_jobs.create(
            target_kind="conversation",
            target_ref=str(conversation_id),
        )

    client.portal.call(seed)

    async def recover() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        handlers = build_agent_cleanup_handlers(repositories, fake_registry)
        report = await run_agent_recovery(
            repositories,
            cleanup_handlers=handlers,
        )
        assert report.cleanup_jobs_completed == 1

    client.portal.call(recover)

    async def verify() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        assert await repositories.agent_conversations.get_by_id(conversation_id) is None
        assert [ref.provider_ref for ref in adapter.deleted_refs] == ["sess_retry"]
        assert await repositories.cleanup_jobs.list_pending() == []

    client.portal.call(verify)
