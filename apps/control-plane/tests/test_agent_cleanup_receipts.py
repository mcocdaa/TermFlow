"""Receipt/coordinator behavior for cleanup manifests (v0.2.0 Task 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.cleanup import (
    AgentCleanupCoordinator,
    CleanupReceiptSeed,
    create_deletion_manifest,
)


def _clock() -> datetime:
    return datetime(2026, 9, 5, tzinfo=UTC)


def test_sqlite_cleanup_pool_enforces_foreign_keys_on_every_connection(tmp_path):
    import asyncio

    from sqlalchemy import text
    from termflow_control_plane.persistence.database import Database

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
        await database.initialize()
        async with database.engine.connect() as first, database.engine.connect() as second:
            assert await first.scalar(text("PRAGMA foreign_keys")) == 1
            assert await second.scalar(text("PRAGMA foreign_keys")) == 1
        await database.dispose()

    asyncio.run(scenario())


async def _manifest(
    client: Any,
    *,
    target_ref: str | None = None,
    seeds: tuple[CleanupReceiptSeed, ...] | None = None,
    handlers: dict[str, Any] | None = None,
) -> tuple[AgentCleanupCoordinator, Any]:
    repositories = RepositoryBundle(client.app.state.session_factory)
    coordinator = AgentCleanupCoordinator(
        repositories,
        handlers or {},
        clock=_clock,
    )
    job = await coordinator.create_or_get_manifest(
        target_kind="term",
        target_ref=target_ref or str(uuid4()),
        receipts=seeds
        or (CleanupReceiptSeed("b_row", "term-row:" + (target_ref or str(uuid4()))),),
        manifest_version=1,
    )
    return coordinator, job


def test_concurrent_manifest_creation_reuses_one_job_and_receipts(client: Any) -> None:
    async def scenario() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        coordinator = AgentCleanupCoordinator(repositories, {}, clock=_clock)
        target_ref = str(uuid4())
        seeds = (
            CleanupReceiptSeed("b_row", "term-row:" + target_ref),
            CleanupReceiptSeed("runtime", "runtime:" + target_ref),
        )
        import asyncio

        jobs = await asyncio.gather(
            *(
                coordinator.create_or_get_manifest(
                    target_kind="term",
                    target_ref=target_ref,
                    receipts=seeds,
                    manifest_version=1,
                )
                for _ in range(2)
            )
        )
        assert jobs[0].id == jobs[1].id
        rows = await repositories.cleanup_jobs.list_receipts(jobs[0].id)
        assert {(row.artifact_kind, row.artifact_ref) for row in rows} == {
            ("b_row", "term-row:" + target_ref),
            ("runtime", "runtime:" + target_ref),
        }

    client.portal.call(scenario)


def test_manifest_progress_requires_every_receipt_before_completion(client: Any) -> None:
    async def scenario() -> None:
        coordinator, job = await _manifest(
            client,
            target_ref=str(uuid4()),
            seeds=(
                CleanupReceiptSeed("one", "one:ref"),
                CleanupReceiptSeed("two", "two:ref"),
            ),
        )
        receipts = await coordinator._repositories.cleanup_jobs.list_receipts(job.id)
        first_receipt = next(row for row in receipts if row.artifact_ref == "one:ref")
        first = await coordinator.confirm_receipt(first_receipt.id, "one:ref", "a" * 64)
        assert first.state == "confirmed"
        state = await coordinator.get_job(job.id)
        assert state is not None and state.state == "pending"
        second = next(
            row
            for row in await coordinator._repositories.cleanup_jobs.list_receipts(job.id)
            if row.artifact_ref == "two:ref"
        )
        await coordinator.confirm_receipt(second.id, "two:ref", "b" * 64)
        state = await coordinator.get_job(job.id)
        assert state is not None and state.state == "completed"

    client.portal.call(scenario)


def test_not_applicable_requires_policy_metadata(client: Any) -> None:
    async def scenario() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        coordinator = AgentCleanupCoordinator(repositories, {}, clock=_clock)
        with pytest.raises(ValueError, match="policy"):
            await coordinator.create_or_get_manifest(
                target_kind="term",
                target_ref=str(uuid4()),
                receipts=(CleanupReceiptSeed("volume", "volume:1", state="not_applicable"),),
                manifest_version=1,
            )

    client.portal.call(scenario)


def test_missing_handler_keeps_receipt_pending_with_backoff(client: Any) -> None:
    async def scenario() -> None:
        coordinator, job = await _manifest(
            client,
            target_ref=str(uuid4()),
            seeds=(CleanupReceiptSeed("missing", "opaque:1"),),
        )
        result = await coordinator.process_due_receipts(job.id, _clock())
        assert result.state == "pending"
        receipt = (await coordinator._repositories.cleanup_jobs.list_receipts(job.id))[0]
        assert receipt.state == "pending"
        assert receipt.attempt_count == 1
        assert receipt.next_attempt_at is not None

    client.portal.call(scenario)


def test_same_evidence_replay_is_idempotent_but_different_evidence_conflicts(client: Any) -> None:
    async def scenario() -> None:
        coordinator, job = await _manifest(
            client,
            target_ref=str(uuid4()),
            seeds=(CleanupReceiptSeed("runtime", "runtime:1"),),
        )
        receipt = (await coordinator._repositories.cleanup_jobs.list_receipts(job.id))[0]
        first = await coordinator.confirm_receipt(receipt.id, "runtime:1", "c" * 64)
        replay = await coordinator.confirm_receipt(receipt.id, "runtime:1", "c" * 64)
        assert replay.id == first.id and replay.evidence_digest == "c" * 64
        with pytest.raises(ValueError, match="evidence"):
            await coordinator.confirm_receipt(receipt.id, "runtime:1", "d" * 64)

    client.portal.call(scenario)


def test_artifact_reference_rejects_credentials_and_payloads(client: Any) -> None:
    async def scenario() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        coordinator = AgentCleanupCoordinator(repositories, {}, clock=_clock)
        for ref in (
            "https://user:pass@example/x",
            "opaque?token=secret",
            '{"api_key":"x"}',
            "token=private",
            "X-Private-Header: value",
            '["provider payload"]',
        ):
            with pytest.raises(ValueError, match="artifact"):
                await coordinator.create_or_get_manifest(
                    target_kind="term",
                    target_ref=str(uuid4()),
                    receipts=(CleanupReceiptSeed("row", ref),),
                    manifest_version=1,
                )

    client.portal.call(scenario)


def test_dead_letter_receipt_never_completes_job(client: Any) -> None:
    async def scenario() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        coordinator = AgentCleanupCoordinator(repositories, {}, clock=_clock)
        job = await coordinator.create_or_get_manifest(
            target_kind="term",
            target_ref=str(uuid4()),
            receipts=(
                CleanupReceiptSeed(
                    "runtime",
                    "runtime:dead",
                    state="pending",
                ),
            ),
            manifest_version=1,
        )
        receipt = (await repositories.cleanup_jobs.list_receipts(job.id))[0]
        await repositories.cleanup_jobs.mark_receipt_dead_letter(
            receipt.id,
            reason="helper rejected",
            now=_clock(),
        )
        refreshed = await coordinator.get_job(job.id)
        assert refreshed is not None and refreshed.state == "dead_letter"
        assert refreshed.completed_at is None

    client.portal.call(scenario)


def test_deletion_manifest_covers_every_owned_artifact_with_explicit_policy(client: Any) -> None:
    async def scenario() -> None:
        repositories = RepositoryBundle(client.app.state.session_factory)
        installation = await repositories.installations.create(digest_secret("cleanup-install"))
        term = await repositories.instances.register_or_rotate(
            uuid4(), installation.id, "cleanup-term", digest_secret("cleanup-term")
        )
        profile = await repositories.agent_profiles.create(
            display_name="cleanup-profile",
            backend_kind="opencode",
            config='{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        )
        binding = await repositories.agent_bindings.create(
            profile_id=profile.id,
            term_id=term.id,
            status="enabled",
            runtime_ref="runtime-cleanup",
            runtime_epoch=3,
            capability_ref="termflow-mcp:cleanup",
        )
        await repositories.agent_runtime_bindings.upsert(
            binding_id=binding.id,
            runtime_ref="runtime-cleanup",
            runtime_epoch=3,
        )
        await repositories.agent_tokens.create(
            binding_id=binding.id,
            token_hash=digest_secret("cleanup-token"),
            scopes=("terminal.observe",),
            expiry_epoch=int((_clock() + timedelta(days=1)).timestamp()),
            binding_epoch=3,
        )
        conversation = await repositories.agent_conversations.create(binding_id=binding.id)
        await repositories.agent_backend_conversations.create(
            conversation_id=conversation.id,
            backend_kind="opencode",
            backend_version="1",
            runtime_id="runtime-cleanup",
            binding_capability_epoch=3,
            provider_ref="session-cleanup",
        )
        await repositories.watches.create(
            binding_id=binding.id,
            conversation_id=conversation.id,
            pane_id="0",
            condition_kind="pane_exited",
            start_cursor="cursor",
            intent_summary="wait",
        )
        await repositories.approvals.create(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id="tool-cleanup",
            canonical_hash="a" * 64,
            auth_epoch=1,
            expires_at=_clock() + timedelta(minutes=5),
        )

        job = await create_deletion_manifest(
            repositories,
            target_kind="binding",
            target_id=binding.id,
        )
        receipts = await repositories.cleanup_jobs.list_receipts(job.id)
        by_kind: dict[str, list[Any]] = {}
        for receipt in receipts:
            by_kind.setdefault(receipt.artifact_kind, []).append(receipt)

        assert {
            "b_row",
            "watch",
            "approval",
            "agent_token",
            "backend_session",
            "runtime_attestation",
            "runtime_volume",
            "container_log",
            "sqlite_wal_backup",
            "provider_retention",
        } <= set(by_kind)
        for kind in ("b_row", "watch", "approval", "agent_token", "backend_session"):
            assert all(row.policy_reason == "b_owned" for row in by_kind[kind])
            assert all(row.policy_version == "v0.2.0" for row in by_kind[kind])
        for kind in (
            "runtime_attestation",
            "runtime_volume",
            "container_log",
            "provider_retention",
        ):
            assert all(row.policy_reason == "deployment_owned" for row in by_kind[kind])
            assert all(row.state == "pending" for row in by_kind[kind])
        assert by_kind["sqlite_wal_backup"][0].state == "not_applicable"
        assert by_kind["sqlite_wal_backup"][0].policy_reason
        assert by_kind["sqlite_wal_backup"][0].policy_version == "v0.2.0"

        result = await AgentCleanupCoordinator(repositories).process_due_receipts(job.id, _clock())
        assert result.state == "pending"
        assert any(
            row.artifact_kind == "runtime_volume"
            and row.state == "pending"
            and row.attempt_count == 1
            for row in result.receipts
        )

    client.portal.call(scenario)
