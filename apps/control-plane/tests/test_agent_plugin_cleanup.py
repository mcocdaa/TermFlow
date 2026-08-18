"""Plugin registration and deletion-cleanup handler tests (plan §15/§17).

Covers the migration manifest (the full alembic chain through the head),
the cleanup-handler registration through the plugin lifecycle, the tombstone
completion path that previously errored forever with "no cleanup handler
registered", and the first-sweep retry of such unhandled jobs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.persistence.models import AgentCleanupJob
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.plugin import (
    CLEANUP_TARGET_KINDS,
    AgentBrokerPlugin,
    build_agent_cleanup_handlers,
    run_agent_recovery,
    run_cleanup_retry,
)


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> tuple[UUID, UUID]:
    """Create a profile + binding for a provisioned Term; returns (term, binding)."""
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model": "default"}',
        },
    )
    assert profile.status_code == 201, profile.text
    term = provision_term(name="cleanup-term")
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": profile.json()["profile_id"], "term_id": str(term.instance_id)},
    )
    assert binding.status_code == 201, binding.text
    return term.instance_id, UUID(str(binding.json()["binding_id"]))


async def _job_state(
    client: TestClient, job_id: UUID
) -> AgentCleanupJob | None:
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        return await session.get(AgentCleanupJob, job_id)


def test_migration_manifest_declares_the_full_alembic_chain(
    client: TestClient,
) -> None:
    """The manifest must own every agent_broker migration through the head
    (review fix: 0009/0010 were missing while the alembic chain was at 0010)."""
    migrations = [
        (revision.id, revision.owner, revision.dependencies)
        for revision in client.app.state.feature_registry.migrations
    ]
    assert migrations == [
        ("0006", "agent_broker", ()),
        ("0007", "agent_broker", ("0006",)),
        ("0008", "agent_broker", ("0007",)),
        ("0009", "agent_broker", ("0008",)),
        ("0010", "agent_broker", ("0009",)),
    ]


def test_plugin_registers_cleanup_handlers_for_every_tombstone_kind(
    client: TestClient,
) -> None:
    plugin: AgentBrokerPlugin = client.app.state.agent_broker_plugin
    handlers = plugin.cleanup_handlers
    assert set(handlers) == set(CLEANUP_TARGET_KINDS)
    assert all(callable(handler) for handler in handlers.values())


def test_recovery_completes_term_tombstone_with_plugin_handlers(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    """A term tombstone previously retried forever with "no cleanup handler
    registered"; the plugin's registered handlers now complete it and sweep
    any surviving Agent rows of the Term."""

    term_id, binding_id = _seed_binding(client, admin_headers, provision_term)

    async def scenario() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        conversation = await repositories.agent_conversations.create(
            binding_id=binding_id, title="sweep"
        )
        watch = await repositories.watches.create(
            binding_id=binding_id,
            conversation_id=conversation.id,
            pane_id="p1",
            condition_kind="output",
            start_cursor="0",
            intent_summary="sweep me",
            state="active",
        )
        job = await repositories.cleanup_jobs.create(
            target_kind="term",
            target_ref=str(term_id),
            term_id=term_id,
        )

        plugin: AgentBrokerPlugin = client.app.state.agent_broker_plugin
        report = await run_agent_recovery(
            repositories,
            cleanup_handlers=plugin.cleanup_handlers,
        )
        assert report.cleanup_jobs_retried == 0
        assert report.cleanup_jobs_completed == 1

        # The handler swept the surviving binding rows and completed the job.
        assert await repositories.agent_bindings.get_by_id(binding_id) is None
        # The watch was swept (cancelled) and then removed with the binding.
        assert await repositories.watches.get_by_id(watch.id) is None
        completed = await _job_state(client, job.id)
        assert completed is not None and completed.state == "completed"

    client.portal.call(scenario)


def test_cleanup_retry_first_sweep_recovers_unhandled_jobs(
    client: TestClient,
) -> None:
    """The plugin's first cleanup sweep re-attempts jobs whose previous
    attempt failed because no handler existed yet, without honoring their
    retry backoff; ordinary sweeps still honor the backoff."""

    async def scenario() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        now = datetime.now(UTC)
        job = await repositories.cleanup_jobs.create(
            target_kind="term",
            target_ref=str(uuid4()),
        )
        # The composition root's pre-startup recovery runs without handlers:
        # the attempt is recorded with a 5-minute backoff.
        report = await run_agent_recovery(repositories, now=now)
        assert report.cleanup_jobs_retried == 1
        assert report.cleanup_jobs_completed == 0
        assert await repositories.cleanup_jobs.list_pending(now=now) == []

        handlers = build_agent_cleanup_handlers(
            repositories, client.app.state.agent_runtime_registry
        )
        # A normal sweep honors the backoff and sees nothing.
        retried, completed = await run_cleanup_retry(
            repositories, handlers=handlers, now=now
        )
        assert (retried, completed) == (0, 0)

        # The first (startup) sweep with retry_unhandled completes it.
        retried, completed = await run_cleanup_retry(
            repositories, handlers=handlers, now=now, retry_unhandled=True
        )
        assert (retried, completed) == (0, 1)
        row = await _job_state(client, job.id)
        assert row is not None and row.state == "completed"

    client.portal.call(scenario)


def test_run_agent_recovery_cleanup_failure_still_records_attempt_and_continues(
    client: TestClient,
) -> None:
    """A failing handler must not abort the sweep: the attempt is recorded
    with the backoff and the next job is still processed (regression guard
    for the step-3 extraction into run_cleanup_retry)."""

    async def scenario() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        now = datetime.now(UTC)
        failing = await repositories.cleanup_jobs.create(
            target_kind="conversation",
            target_ref=str(uuid4()),
        )
        succeeding = await repositories.cleanup_jobs.create(
            target_kind="term",
            target_ref=str(uuid4()),
        )

        async def _fail(job: AgentCleanupJob) -> None:
            raise RuntimeError("backend unreachable")

        report = await run_agent_recovery(
            repositories,
            now=now,
            cleanup_handlers={
                "conversation": _fail,
                "term": build_agent_cleanup_handlers(
                    repositories, client.app.state.agent_runtime_registry
                )["term"],
            },
        )
        assert report.cleanup_jobs_retried == 1
        assert report.cleanup_jobs_completed == 1

        failed_row = await _job_state(client, failing.id)
        assert failed_row is not None
        assert failed_row.state == "pending"
        assert failed_row.attempt_count == 1
        assert failed_row.last_error == "backend unreachable"
        succeeded_row = await _job_state(client, succeeding.id)
        assert succeeded_row is not None and succeeded_row.state == "completed"

    client.portal.call(scenario)


def test_run_agent_recovery_fences_inbox_then_runs_then_cleanup(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    """The plan §17 restart order is preserved by the refactor: stale inbox
    claims and stuck runs are fenced before cleanup handlers run."""

    term_id, binding_id = _seed_binding(client, admin_headers, provision_term)

    async def scenario() -> None:
        repositories: RepositoryBundle = client.app.state.repositories
        now = datetime.now(UTC)
        conversation = await repositories.agent_conversations.create(
            binding_id=binding_id, title="order"
        )
        item = await repositories.agent_inbox.enqueue(
            conversation_id=conversation.id,
            kind="user_message",
            actor_id="actor-1",
            actor_kind="user_session",
            idempotency_key="stale-claim",
            payload_digest="d" * 64,
            source="user",
            now=now,
        )
        await repositories.agent_inbox.claim(
            item.id, "crashed-worker", lease_seconds=60, now=now
        )
        later = now + timedelta(minutes=5)
        run = await repositories.agent_runs.create(
            conversation_id=conversation.id,
            run_state="running",
            started_at=later,
        )
        await repositories.cleanup_jobs.create(
            target_kind="term",
            target_ref=str(term_id),
            term_id=term_id,
        )

        observed: list[str] = []

        async def _term_handler(job: AgentCleanupJob) -> None:
            assert (
                await repositories.agent_runs.get_by_id(run.id)
            ).run_state == "unknown", "cleanup ran before run fencing"
            observed.append("cleanup")

        report = await run_agent_recovery(
            repositories,
            now=later,
            cleanup_handlers={"term": _term_handler},
        )
        assert report.inbox_recovered == 1
        assert report.runs_marked_unknown == 1
        assert report.cleanup_jobs_completed == 1
        assert observed == ["cleanup"]

    client.portal.call(scenario)
