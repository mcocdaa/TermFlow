"""HTTP cleanup-job status and 202 deletion contracts (Task 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.cleanup import (
    AgentCleanupCoordinator,
    CleanupReceiptSeed,
)


def test_cleanup_job_status_exposes_safe_receipt_state_without_refs(
    client: Any,
    admin_headers: dict[str, str],
) -> None:
    async def seed() -> UUID:
        repositories = RepositoryBundle(client.app.state.session_factory)
        coordinator = AgentCleanupCoordinator(repositories)
        job = await coordinator.create_or_get_manifest(
            target_kind="installation",
            target_ref=str(uuid4()),
            receipts=(CleanupReceiptSeed("container", "container:opaque"),),
        )
        await repositories.cleanup_jobs.record_attempt(
            job.id,
            next_attempt_at=datetime.now(UTC),
            last_error="internal secret=should-not-leak",
        )
        return job.id

    job_id = client.portal.call(seed)
    response = client.get(f"/api/v1/agent/admin/cleanup-jobs/{job_id}", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cleanup_job_id"] == str(job_id)
    assert body["state"] == "pending"
    assert "container:opaque" not in response.text
    assert "should-not-leak" not in response.text
    assert all("artifact_ref" not in receipt for receipt in body["receipts"])


def test_pending_delete_returns_202_and_reuses_job(
    client: Any,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "cleanup-api-profile",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    assert profile.status_code == 201, profile.text
    term = provision_term(name="cleanup-api-term")
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={
            "profile_id": profile.json()["profile_id"],
            "term_id": str(term.instance_id),
        },
    )
    assert binding.status_code == 201, binding.text
    binding_id = UUID(binding.json()["binding_id"])
    conversation = client.post(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        json={"binding_id": str(binding_id)},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])

    async def seed_backend() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        await repositories.agent_backend_conversations.create(
            conversation_id=conversation_id,
            backend_kind="opencode",
            backend_version="0.3.0",
            runtime_id="runtime-1",
            binding_capability_epoch=1,
            provider_ref="opaque-session",
        )

    client.portal.call(seed_backend)

    # Deliberately leave the runtime registry without a mapped pipeline: the
    # backend deletion is unprovable and must remain visibly pending.
    class EmptyRegistry:
        def pipeline_for(self, _binding_id: UUID) -> None:
            return None

    client.app.state.agent_runtime_registry = EmptyRegistry()
    first = client.delete(f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["state"] == "deletion_pending"
    assert set(body) == {"cleanup_job_id", "state", "status_url"}
    second = client.delete(f"/api/v1/agent/conversations/{conversation_id}", headers=admin_headers)
    assert second.status_code == 202, second.text
    assert second.json()["cleanup_job_id"] == body["cleanup_job_id"]


def test_cleanup_helper_auth_reference_digest_and_idempotency(client, admin_headers):
    client.app.state.settings.agent_cleanup_helper_token = SecretStr(
        "independent-helper-token-for-tests"
    )

    async def seed():
        repositories = RepositoryBundle(client.app.state.session_factory)
        job = await AgentCleanupCoordinator(repositories).create_or_get_manifest(
            target_kind="installation",
            target_ref=str(uuid4()),
            receipts=[CleanupReceiptSeed("container", "container:opaque")],
        )
        return job.id, (await repositories.cleanup_jobs.list_receipts(job.id))[0].id

    job_id, receipt_id = client.portal.call(seed)
    path = f"/api/v1/agent/admin/cleanup-jobs/{job_id}/receipts/{receipt_id}/confirm"
    payload = dict(
        artifact_ref="container:opaque",
        result="confirmed",
        evidence_digest="a" * 64,
        reason_code=None,
        idempotency_key=str(uuid4()),
    )
    helper = {"Authorization": "Bearer independent-helper-token-for-tests"}
    for headers in ({}, admin_headers):
        assert client.post(path, headers=headers, json=payload).status_code == 401
    for digest in ("a" * 63, "z" * 64):
        assert (
            client.post(
                path, headers=helper, json={**payload, "evidence_digest": digest}
            ).status_code
            == 422
        )
    assert (
        client.post(path, headers=helper, json={**payload, "artifact_ref": "other"}).status_code
        == 409
    )
    assert client.post(path, headers=helper, json=payload).status_code == 200
    assert client.post(path, headers=helper, json=payload).status_code == 200
    assert (
        client.post(path, headers=helper, json={**payload, "evidence_digest": "b" * 64}).status_code
        == 409
    )
    assert (
        client.post(
            path, headers=helper, json={**payload, "idempotency_key": str(uuid4())}
        ).status_code
        == 409
    )
    assert (
        client.get(f"/api/v1/agent/admin/cleanup-jobs/{job_id}", headers=helper).status_code == 401
    )


@pytest.mark.parametrize("kind", ["term", "installation"])
def test_parent_delete_reuses_manifest_after_cascade(client, admin_headers, provision_term, kind):
    term = provision_term(name="delete-manifest")
    target = term.instance_id if kind == "term" else term.computer.installation_id
    path = f"/api/v1/{'terms' if kind == 'term' else 'computers'}/{target}"
    first = client.delete(path, headers=admin_headers)
    assert first.status_code == 202, first.text
    second = client.delete(path, headers=admin_headers)
    assert second.status_code == 202, second.text
    assert second.json() == first.json()


def test_parent_manifest_preserves_external_receipts_until_helper_confirmation(
    client, admin_headers, provision_term
):
    term = provision_term(name="external-cleanup")
    client.app.state.settings.agent_cleanup_helper_token = SecretStr(
        "independent-helper-token-for-tests"
    )

    async def seed():
        repositories = client.app.state.repositories
        profile = await repositories.agent_profiles.create(
            display_name="external", backend_kind="opencode", config="{}"
        )
        await repositories.agent_bindings.create(
            profile_id=profile.id,
            term_id=term.instance_id,
            runtime_ref="runtime-external",
            runtime_epoch=1,
        )

    client.portal.call(seed)
    path = f"/api/v1/terms/{term.instance_id}"
    first = client.delete(path, headers=admin_headers)
    assert first.status_code == 202
    job_id = UUID(first.json()["cleanup_job_id"])

    async def internal_done():
        repositories = client.app.state.repositories
        await repositories.cleanup_jobs.confirm_pending_internal(job_id)
        job = await repositories.cleanup_jobs.refresh_state(job_id)
        assert job.state == "pending"
        return await repositories.cleanup_jobs.list_receipts(job_id)

    receipts = client.portal.call(internal_done)
    external = [
        row
        for row in receipts
        if row.state == "pending" and row.policy_reason == "deployment_owned"
    ]
    assert {row.artifact_kind for row in external} == {
        "runtime_attestation",
        "runtime_volume",
        "container_log",
        "sqlite_wal_backup",
    }
    assert all(row.state == "pending" for row in external)
    assert {
        row.artifact_ref
        for row in external
        if row.artifact_kind != "sqlite_wal_backup"
    } == {"runtime-external"}
    for receipt in external:
        response = client.post(
            f"/api/v1/agent/admin/cleanup-jobs/{job_id}/receipts/{receipt.id}/confirm",
            headers={"Authorization": "Bearer independent-helper-token-for-tests"},
            json=dict(
                artifact_ref=receipt.artifact_ref,
                result="confirmed",
                evidence_digest="a" * 64,
                idempotency_key=str(uuid4()),
            ),
        )
        assert response.status_code == 200, response.text
    assert client.delete(path, headers=admin_headers).status_code == 204
