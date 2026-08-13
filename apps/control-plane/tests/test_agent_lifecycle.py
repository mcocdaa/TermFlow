"""Agent Broker lifecycle tests (plan §15, §17; task M1.6).

Covers the M1.6 verification wave:

* startup purge/rebuild integration: ``purge_expired`` sweeps every Agent
  persistence class except approvals (tokens, transcript drafts,
  diagnostics, watches, terminal inbox/run rows, confirmed cleanup
  tombstones); expired approvals sweep through ``ApprovalPolicy`` at the
  composition root so each swept row records an ``expired`` audit event.
* deterministic restart recovery order
  (``run_agent_recovery``: inbox claims -> stuck runs -> cleanup jobs)
  including fail-safe error handling.
* durable deletion hooks in the Term and installation deletion paths:
  active watches are cancelled and a ``agent_cleanup_jobs`` tombstone is
  created BEFORE the parent row is deleted, so the job survives (SET NULL).
* token expiry/revocation: expired tokens are purged and revoked tokens are
  excluded from lookups (fail closed).
* cross-Term identity isolation: bindings, conversations, tokens, watches,
  inbox sequences, event cursors, and message revisions never leak across
  Terms.
* the M1 exit gate: B durably accepts a typed user input and replays the
  canonical product timeline through the product APIs using a fake backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentCleanupJob, TranscriptDraft
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
    InboxEnvelope,
)
from termflow_control_plane.plugins.agent_broker.agent.runs import AgentRunStateMachine
from termflow_control_plane.plugins.agent_broker.agent.turns import BackendOutcome
from termflow_control_plane.plugins.agent_broker.plugin import run_agent_recovery


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Clock:
    """Mutable time source so tests can drive lease expiry deterministically."""

    def __init__(self, start: datetime) -> None:
        self._value = start

    def __call__(self) -> datetime:
        return self._value

    def advance(self, **delta: float) -> None:
        self._value += timedelta(**delta)


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


def _portal_call(
    client: Any,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run an async call with kwargs through the TestClient's portal."""

    async def run() -> Any:
        return await func(*args, **kwargs)

    return client.portal.call(run)


async def _get_cleanup_job(
    repos: RepositoryBundle, job_id: UUID
) -> AgentCleanupJob | None:
    async with repos.session_factory() as session:  # type: ignore[attr-defined]
        return await session.get(AgentCleanupJob, job_id)


async def _list_draft_states(repos: RepositoryBundle) -> set[str]:
    async with repos.session_factory() as session:  # type: ignore[attr-defined]
        rows = await session.scalars(select(TranscriptDraft))
        return {draft.state for draft in rows}


async def _seed_profile(repos: RepositoryBundle, name: str | None = None) -> UUID:
    profile = await repos.agent_profiles.create(
        display_name=name or f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    return profile.id


async def _seed_term(repos: RepositoryBundle, name: str | None = None) -> UUID:
    display_name = name or f"term-{uuid4().hex[:8]}"
    installation = await repos.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repos.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    return term.id


async def _seed_binding(
    repos: RepositoryBundle,
    *,
    profile_id: UUID,
    term_id: UUID,
) -> UUID:
    binding = await repos.agent_bindings.create(
        profile_id=profile_id,
        term_id=term_id,
        status="pending",
    )
    return binding.id


async def _seed_conversation(
    repos: RepositoryBundle,
    *,
    binding_id: UUID,
    title: str | None = None,
) -> UUID:
    conversation = await repos.agent_conversations.create(
        binding_id=binding_id,
        title=title,
    )
    return conversation.id


async def _seed_term_with_conversation(
    repos: RepositoryBundle,
) -> tuple[UUID, UUID, UUID, UUID]:
    """Profile, Term, binding, and conversation sharing one Term identity."""
    profile_id = await _seed_profile(repos)
    term_id = await _seed_term(repos)
    binding_id = await _seed_binding(repos, profile_id=profile_id, term_id=term_id)
    conversation_id = await _seed_conversation(repos, binding_id=binding_id)
    return profile_id, term_id, binding_id, conversation_id


def _machine(
    repos: RepositoryBundle,
    *,
    clock: Clock,
    capabilities: AgentBackendCapabilities | None = None,
) -> InboxDeliveryStateMachine:
    return InboxDeliveryStateMachine(
        repos.agent_inbox,
        now=clock,
        capabilities=capabilities,
        worker_id="test-worker",
    )


# ---------------------------------------------------------------------------
# Purge integration (plan §15): one startup sweep covers every Agent class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_expired_sweeps_every_agent_expiry_class(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    binding_id = await _seed_binding(
        repositories,
        profile_id=await _seed_profile(repositories),
        term_id=await _seed_term(repositories),
    )
    conversation_id = await _seed_conversation(repositories, binding_id=binding_id)

    # Expired token + a token that is still valid.
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("expired-token"),
        scopes=("observe",),
        expiry_epoch=int((observed - timedelta(minutes=5)).timestamp()),
        binding_epoch=1,
    )
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("valid-token"),
        scopes=("observe",),
        expiry_epoch=int((observed + timedelta(days=1)).timestamp()),
        binding_epoch=1,
    )
    # Expired approval + one still pending: both must survive this sweep
    # (approvals are finished by ApprovalPolicy at the composition root).
    await repositories.approvals.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id="expired-call",
        canonical_hash=_hash("expired"),
        auth_epoch=1,
        expires_at=observed - timedelta(minutes=5),
    )
    await repositories.approvals.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id="valid-call",
        canonical_hash=_hash("valid"),
        auth_epoch=1,
        expires_at=observed + timedelta(minutes=5),
    )
    # Expired transcript draft + one still pending.
    await repositories.transcript_drafts.create(
        binding_id=binding_id,
        target_conversation_id=conversation_id,
        owner_actor_id="actor-1",
        transcript_hash=_hash("expired"),
        provider="whisper",
        region="us-east-1",
        expires_at=observed - timedelta(minutes=5),
    )
    await repositories.transcript_drafts.create(
        binding_id=binding_id,
        target_conversation_id=conversation_id,
        owner_actor_id="actor-1",
        transcript_hash=_hash("valid"),
        provider="whisper",
        region="us-east-1",
        expires_at=observed + timedelta(minutes=5),
    )
    # Expired diagnostic + one still live.
    await repositories.diagnostics.append(
        conversation_id=conversation_id,
        event_kind="backend_state_changed",
        correlation_hash=_hash("expired"),
        ttl_expires_at=observed - timedelta(minutes=5),
    )
    await repositories.diagnostics.append(
        conversation_id=conversation_id,
        event_kind="backend_state_changed",
        correlation_hash=_hash("valid"),
        ttl_expires_at=observed + timedelta(minutes=5),
    )
    # Expired watch + one still active.
    await repositories.watches.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        pane_id="term-a:0",
        condition_kind="output_contains",
        start_cursor="stream:1:1",
        intent_summary="expired",
        expiry_at=observed - timedelta(minutes=5),
    )
    await repositories.watches.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        pane_id="term-a:1",
        condition_kind="output_contains",
        start_cursor="stream:1:2",
        intent_summary="valid",
        expiry_at=observed + timedelta(minutes=5),
    )

    counts = await repositories.purge_expired(now=observed)

    assert counts["agent_tokens"] == 1
    assert counts["transcript_drafts"] == 1
    assert counts["diagnostics"] == 1
    assert counts["watches"] == 1
    assert counts["agent_inbox"] == 0
    assert counts["agent_runs"] == 0
    assert counts["cleanup_jobs"] == 0
    # Approvals are not swept here: the composition root sweeps them through
    # ApprovalPolicy so every row records an `expired` audit event (§5/§7).
    assert "approvals" not in counts

    # Expired rows are gone or expired in place; valid rows survive.
    assert await repositories.agent_tokens.get_by_hash(_hash("expired-token")) is None
    assert await repositories.agent_tokens.get_by_hash(_hash("valid-token")) is not None
    # The expired approval stays pending: this sweep never touches it.
    assert (
        await repositories.approvals.get_by_tool_call(conversation_id, "expired-call")
    ).state == "pending"
    assert (
        await repositories.approvals.get_by_tool_call(conversation_id, "valid-call")
    ).state == "pending"
    # The expired draft is expired in place; the valid one stays pending.
    assert await _list_draft_states(repositories) == {"expired", "pending"}
    active_watches = await repositories.watches.list_active(now=observed)
    assert [watch.intent_summary for watch in active_watches] == ["valid"]


@pytest.mark.asyncio
async def test_app_startup_sweeps_expired_approvals_through_policy_with_audit(
    tmp_path,
) -> None:
    """The production expiry sweep goes through ``ApprovalPolicy`` (spec §5/§7).

    Seeding rows before startup and running the real composition-root
    lifespan proves the wired path: an expired approval is finished as
    ``expired`` WITH an ``expired`` audit event (the policy records one per
    swept row), while a still-valid approval is untouched by the sweep (the
    restart recovery then finishes it as ``revoked``, never ``expired``).
    """
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    profile_id = await _seed_profile(repositories)
    term_id = await _seed_term(repositories)
    binding_id = await _seed_binding(repositories, profile_id=profile_id, term_id=term_id)
    conversation_id = await _seed_conversation(repositories, binding_id=binding_id)
    now = datetime.now(UTC)
    expired = await repositories.approvals.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id="expired-call",
        canonical_hash=_hash("expired"),
        auth_epoch=1,
        expires_at=now - timedelta(minutes=5),
    )
    valid = await repositories.approvals.create(
        binding_id=binding_id,
        conversation_id=conversation_id,
        tool_call_id="valid-call",
        canonical_hash=_hash("valid"),
        auth_epoch=1,
        expires_at=now + timedelta(minutes=5),
    )

    app = create_app(
        settings=Settings(
            admin_token="admin-token-that-is-long-enough-for-tests",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
            allow_insecure_loopback=True,
        ),
        database=database,
    )
    async with app.router.lifespan_context(app):
        pass

    # The swept approval expired through the policy: state and audit.
    swept = await repositories.approvals.get_by_id(expired.id)
    assert swept is not None
    assert swept.state == "expired"
    swept_events = await repositories.approval_audit.list_for_approval(expired.id)
    assert [event.event_type for event in swept_events] == ["expired"]
    # The still-valid approval was never expired by the sweep; the restart
    # recovery finishes it as revoked with its own audit event.
    survivor = await repositories.approvals.get_by_id(valid.id)
    assert survivor is not None
    assert survivor.state == "revoked"
    survivor_events = await repositories.approval_audit.list_for_approval(valid.id)
    assert [event.event_type for event in survivor_events] == ["revoked"]
    await database.dispose()


@pytest.mark.asyncio
async def test_purge_expired_sweeps_terminal_inbox_runs_and_cleanup_tombstones(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    old = observed - timedelta(days=31)
    binding_id = await _seed_binding(
        repositories,
        profile_id=await _seed_profile(repositories),
        term_id=await _seed_term(repositories),
    )
    conversation_id = await _seed_conversation(repositories, binding_id=binding_id)

    # Old terminal inbox rows (dead_letter/cancelled) and a fresh one.
    old_dead = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="old-dead",
        payload_digest=_hash("old-dead"),
        source="user",
        now=old,
    )
    old_cancelled = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="old-cancelled",
        payload_digest=_hash("old-cancelled"),
        source="user",
        now=old,
    )
    fresh_dead = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="fresh-dead",
        payload_digest=_hash("fresh-dead"),
        source="user",
        now=observed,
    )
    await repositories.agent_inbox.dead_letter(old_dead.id)
    await repositories.agent_inbox.mark_cancelled(old_cancelled.id)
    await repositories.agent_inbox.dead_letter(fresh_dead.id)

    # Old terminal runs and a fresh one.
    old_run = await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="running", started_at=old
    )
    await repositories.agent_runs.set_state(
        old_run.id, "completed", expected_state="running", now=old
    )
    fresh_run = await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="running", started_at=observed
    )
    await repositories.agent_runs.set_state(
        fresh_run.id, "failed", expected_state="running", now=observed
    )

    # An old confirmed cleanup tombstone and a fresh one.
    old_job = await repositories.cleanup_jobs.create(
        target_kind="term",
        target_ref=str(uuid4()),
    )
    fresh_job = await repositories.cleanup_jobs.create(
        target_kind="term",
        target_ref=str(uuid4()),
    )
    await repositories.cleanup_jobs.complete(old_job.id, now=old)
    await repositories.cleanup_jobs.complete(fresh_job.id, now=observed)

    counts = await repositories.purge_expired(now=observed)

    assert counts["agent_inbox"] == 2
    assert counts["agent_runs"] == 1
    assert counts["cleanup_jobs"] == 1
    assert counts["agent_tokens"] == 0

    remaining_inbox = await repositories.agent_inbox.list_for_conversation(
        conversation_id
    )
    assert [item.id for item in remaining_inbox] == [fresh_dead.id]
    remaining_runs = await repositories.agent_runs.list_for_conversation(conversation_id)
    assert [run.id for run in remaining_runs] == [fresh_run.id]
    assert await _get_cleanup_job(repositories, old_job.id) is None
    assert await _get_cleanup_job(repositories, fresh_job.id) is not None


# ---------------------------------------------------------------------------
# Restart recovery order (plan §17: B restarts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_recovery_recovers_stale_claims_and_marks_stuck_runs_unknown(
    repositories: RepositoryBundle,
) -> None:
    clock = Clock(datetime.now(UTC))
    _, _, _, conversation_id = await _seed_term_with_conversation(repositories)

    # A claim whose lease expired while B was down.
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="stale-claim",
        payload_digest=_hash("stale"),
        source="user",
        now=clock(),
    )
    await repositories.agent_inbox.claim(
        item.id, "crashed-worker", lease_seconds=60, now=clock()
    )
    clock.advance(minutes=5)

    # A run that was mid-flight when B crashed.
    run = await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="running", started_at=clock()
    )

    report = await run_agent_recovery(repositories, now=clock())

    assert report.inbox_recovered == 1
    assert report.runs_marked_unknown == 1
    assert report.cleanup_jobs_retried == 0
    assert report.cleanup_jobs_completed == 0

    # The stale claim is claimable again (restored to pending by recovery).
    claimable = await repositories.agent_inbox.next_pending(
        conversation_id=conversation_id, now=clock()
    )
    assert [row.id for row in claimable] == [item.id]
    # The stuck run is fenced to unknown.
    assert (await repositories.agent_runs.get_by_id(run.id)).run_state == "unknown"


@pytest.mark.asyncio
async def test_run_agent_recovery_order_inbox_then_runs_then_cleanup(
    repositories: RepositoryBundle,
) -> None:
    clock = Clock(datetime.now(UTC))
    _, _, _, conversation_id = await _seed_term_with_conversation(repositories)

    await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="order-item",
        payload_digest=_hash("order"),
        source="user",
        now=clock(),
    )
    run = await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="running", started_at=clock()
    )
    await repositories.cleanup_jobs.create(
        target_kind="backend_conversation",
        target_ref="sess_abc123",
    )

    order: list[str] = []

    async def cleanup_handler(job_row: AgentCleanupJob) -> None:
        # By the time cleanup runs, inbox recovery and run fencing must have
        # already taken effect.
        assert (
            await repositories.agent_runs.get_by_id(run.id)
        ).run_state == "unknown", "cleanup ran before run fencing"
        order.append("cleanup")

    class RecordingRuns:
        """Records when the run fencing step runs."""

        def __init__(self, inner: AgentRunStateMachine) -> None:
            self._inner = inner

        async def mark_unknown(self, run_id: UUID) -> None:
            order.append("runs")
            await self._inner.mark_unknown(run_id)

    report = await run_agent_recovery(
        repositories,
        now=clock(),
        inbox_machine=_machine(repositories, clock=clock),
        run_machine=RecordingRuns(AgentRunStateMachine(repositories.agent_runs)),  # type: ignore[arg-type]
        cleanup_handlers={"backend_conversation": cleanup_handler},
    )

    assert report.runs_marked_unknown == 1
    assert report.cleanup_jobs_completed == 1
    assert order == ["runs", "cleanup"]
    assert (await repositories.agent_runs.get_by_id(run.id)).run_state == "unknown"


@pytest.mark.asyncio
async def test_run_agent_recovery_retries_pending_cleanup_jobs(
    repositories: RepositoryBundle,
) -> None:
    clock = Clock(datetime.now(UTC))
    job = await repositories.cleanup_jobs.create(
        target_kind="term",
        target_ref=str(uuid4()),
    )

    # No handler registered: the retry is recorded with a backoff.
    report = await run_agent_recovery(repositories, now=clock())
    assert report.cleanup_jobs_retried == 1
    assert report.cleanup_jobs_completed == 0
    # The job is not pending again until the backoff has passed.
    assert await repositories.cleanup_jobs.list_pending(now=clock()) == []
    clock.advance(minutes=6)
    retried = (await repositories.cleanup_jobs.list_pending(now=clock()))[0]
    assert retried.id == job.id
    assert retried.state == "pending"
    assert retried.attempt_count == 1
    assert "no cleanup handler" in retried.last_error

    # A registered handler that succeeds completes the job.
    async def cleanup_handler(job_row: AgentCleanupJob) -> None:
        return None

    report = await run_agent_recovery(
        repositories,
        now=clock(),
        cleanup_handlers={"term": cleanup_handler},
    )
    assert report.cleanup_jobs_retried == 0
    assert report.cleanup_jobs_completed == 1
    assert await repositories.cleanup_jobs.list_pending(now=clock()) == []


@pytest.mark.asyncio
async def test_run_agent_recovery_cleanup_failure_records_attempt_and_continues(
    repositories: RepositoryBundle,
    caplog,
) -> None:
    clock = Clock(datetime.now(UTC))
    failing = await repositories.cleanup_jobs.create(
        target_kind="backend_conversation",
        target_ref="sess_fail",
    )
    succeeding = await repositories.cleanup_jobs.create(
        target_kind="volume",
        target_ref="/data/term-a",
    )

    async def failing_handler(job_row: AgentCleanupJob) -> None:
        raise RuntimeError("backend unreachable")

    async def succeeding_handler(job_row: AgentCleanupJob) -> None:
        return None

    report = await run_agent_recovery(
        repositories,
        now=clock(),
        cleanup_handlers={
            "backend_conversation": failing_handler,
            "volume": succeeding_handler,
        },
    )

    assert report.cleanup_jobs_retried == 1
    assert report.cleanup_jobs_completed == 1
    failed_row = await _get_cleanup_job(repositories, failing.id)
    assert failed_row.state == "pending"
    assert failed_row.attempt_count == 1
    assert failed_row.last_error == "backend unreachable"
    completed_row = await _get_cleanup_job(repositories, succeeding.id)
    assert completed_row is not None and completed_row.state == "completed"
    # The error is logged, and the failure never aborts the sweep.
    assert any("backend unreachable" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_run_agent_recovery_is_fail_safe_when_machines_raise(
    repositories: RepositoryBundle,
    caplog,
) -> None:
    clock = Clock(datetime.now(UTC))
    _, _, _, conversation_id = await _seed_term_with_conversation(repositories)
    await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="running", started_at=clock()
    )

    class ExplodingInbox:
        async def recover_stale(self, **kwargs: Any) -> list[InboxEnvelope]:
            raise RuntimeError("inbox recovery exploded")

    class ExplodingRuns:
        async def mark_unknown(self, run_id: UUID) -> None:
            raise RuntimeError("run fencing exploded")

    report = await run_agent_recovery(
        repositories,
        now=clock(),
        inbox_machine=ExplodingInbox(),  # type: ignore[arg-type]
        run_machine=ExplodingRuns(),  # type: ignore[arg-type]
    )

    assert report.inbox_recovered == 0
    assert report.runs_marked_unknown == 0
    assert report.cleanup_jobs_retried == 0
    # Recovery completes without raising; each failure is logged.
    assert any("inbox recovery exploded" in record.message for record in caplog.records)
    assert any("run fencing exploded" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Deletion hooks (plan §15, §17: Term deleted)
# ---------------------------------------------------------------------------


def _seed_profile_binding_conversation_watch(
    client: Any,
    term_id: UUID,
) -> tuple[UUID, UUID]:
    repos = client.app.state.repositories
    profile = _portal_call(
        client,
        repos.agent_profiles.create,
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    binding = _portal_call(
        client,
        repos.agent_bindings.create,
        profile_id=profile.id,
        term_id=term_id,
        status="pending",
    )
    conversation = _portal_call(
        client,
        repos.agent_conversations.create,
        binding_id=binding.id,
        title="cleanup-test",
    )
    watch = _portal_call(
        client,
        repos.watches.create,
        binding_id=binding.id,
        conversation_id=conversation.id,
        pane_id="term-a:0",
        condition_kind="output_contains",
        start_cursor="stream:1:1",
        intent_summary="wait for build",
    )
    return conversation.id, watch.id


def test_delete_term_creates_cleanup_tombstone_and_cancels_watches(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(hostname="term-host", name="before")
    instance_id = term.instance_id
    repos = client.app.state.repositories

    conversation_id, watch_id = _seed_profile_binding_conversation_watch(
        client, instance_id
    )

    response = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
    assert response.status_code == 204

    # The Term is gone...
    assert _portal_call(client, repos.instances.get, instance_id) is None
    # ...the tombstone was created BEFORE the deletion and survived it
    # (target_ref stays, term_id is SET NULL).
    jobs = _portal_call(client, repos.cleanup_jobs.list_pending)
    matching = [job for job in jobs if job.target_ref == str(instance_id)]
    assert len(matching) == 1
    assert matching[0].target_kind == "term"
    assert matching[0].state == "pending"
    assert matching[0].term_id is None
    # Watches are cancelled by the hook and then cascade away with the
    # binding: no active watch may survive the Term.
    assert _portal_call(client, repos.watches.get_by_id, watch_id) is None
    assert _portal_call(client, repos.watches.list_active) == []
    assert (
        _portal_call(client, repos.agent_conversations.get_by_id, conversation_id)
        is None
    )


def test_delete_computer_creates_installation_cleanup_tombstone_and_cancels_watches(
    client,
    admin_headers,
    provision_computer,
    provision_term,
) -> None:
    computer = provision_computer(hostname="term-host")
    term = provision_term(computer=computer, name="before")
    repos = client.app.state.repositories

    _, watch_id = _seed_profile_binding_conversation_watch(client, term.instance_id)

    response = client.delete(
        f"/api/v1/computers/{computer.installation_id}",
        headers=admin_headers,
    )
    assert response.status_code == 204

    jobs = _portal_call(client, repos.cleanup_jobs.list_pending)
    matching = [job for job in jobs if job.target_ref == str(computer.installation_id)]
    assert len(matching) == 1
    assert matching[0].target_kind == "installation"
    assert matching[0].state == "pending"
    # Installation deletion revokes the row rather than deleting it, so the
    # tombstone keeps its FK link; either way it survives the parent.
    assert matching[0].installation_id == computer.installation_id
    assert (
        _portal_call(client, repos.installations.get, computer.installation_id).revoked_at
        is not None
    )
    # Watches are cancelled by the hook and cascade away with the binding.
    assert _portal_call(client, repos.watches.get_by_id, watch_id) is None
    assert _portal_call(client, repos.watches.list_active) == []


# ---------------------------------------------------------------------------
# Token expiry/revocation (plan §17: fail closed after revocation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_expired_removes_expired_tokens_and_keeps_valid_tokens(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    binding_id = await _seed_binding(
        repositories,
        profile_id=await _seed_profile(repositories),
        term_id=await _seed_term(repositories),
    )
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("expired"),
        scopes=("observe",),
        expiry_epoch=int((observed - timedelta(seconds=1)).timestamp()),
        binding_epoch=1,
    )
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("still-valid"),
        scopes=("observe",),
        expiry_epoch=int((observed + timedelta(hours=1)).timestamp()),
        binding_epoch=1,
    )

    counts = await repositories.purge_expired(now=observed)

    assert counts["agent_tokens"] == 1
    assert await repositories.agent_tokens.get_by_hash(_hash("expired")) is None
    assert (
        await repositories.agent_tokens.get_by_hash(_hash("still-valid")) is not None
    )
    listed = await repositories.agent_tokens.list_for_binding(binding_id)
    assert [token.token_hash for token in listed] == [_hash("still-valid")]


@pytest.mark.asyncio
async def test_revoked_token_is_excluded_from_lookups(
    repositories: RepositoryBundle,
) -> None:
    binding_id = await _seed_binding(
        repositories,
        profile_id=await _seed_profile(repositories),
        term_id=await _seed_term(repositories),
    )
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("revoked-token"),
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )
    await repositories.agent_tokens.create(
        binding_id=binding_id,
        token_hash=_hash("live-token"),
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )

    assert await repositories.agent_tokens.revoke(_hash("revoked-token")) is True
    # Fail closed: a revoked token never authenticates again.
    assert await repositories.agent_tokens.get_by_hash(_hash("revoked-token")) is None
    assert await repositories.agent_tokens.get_by_hash(_hash("live-token")) is not None
    # The revocation stays visible in the binding's token history.
    listed = await repositories.agent_tokens.list_for_binding(binding_id)
    by_hash = {token.token_hash: token for token in listed}
    assert by_hash[_hash("revoked-token")].revoked_at is not None
    assert by_hash[_hash("live-token")].revoked_at is None


# ---------------------------------------------------------------------------
# Cross-Term identity isolation (plan §4.2, §13.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_term_identity_isolation(repositories: RepositoryBundle) -> None:
    profile_id = await _seed_profile(repositories, name="shared-profile")
    term_a = await _seed_term(repositories, name="term-a")
    term_b = await _seed_term(repositories, name="term-b")
    binding_a = await _seed_binding(repositories, profile_id=profile_id, term_id=term_a)
    binding_b = await _seed_binding(repositories, profile_id=profile_id, term_id=term_b)
    conversation_a = await _seed_conversation(repositories, binding_id=binding_a)
    conversation_b = await _seed_conversation(repositories, binding_id=binding_b)

    # Bindings are scoped per Term.
    assert [
        binding.id for binding in await repositories.agent_bindings.list_for_term(term_a)
    ] == [binding_a]
    assert [
        binding.id for binding in await repositories.agent_bindings.list_for_term(term_b)
    ] == [binding_b]

    # Conversations are scoped per binding (never cross-Term).
    assert [
        conversation.id
        for conversation in await repositories.agent_conversations.list_for_binding(
            binding_a
        )
    ] == [conversation_a]
    assert [
        conversation.id
        for conversation in await repositories.agent_conversations.list_for_binding(
            binding_b
        )
    ] == [conversation_b]

    # Tokens and watches are scoped per binding.
    await repositories.agent_tokens.create(
        binding_id=binding_a,
        token_hash=_hash("token-a"),
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )
    await repositories.agent_tokens.create(
        binding_id=binding_b,
        token_hash=_hash("token-b"),
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )
    assert [
        token.token_hash
        for token in await repositories.agent_tokens.list_for_binding(binding_a)
    ] == [_hash("token-a")]
    assert [
        token.token_hash
        for token in await repositories.agent_tokens.list_for_binding(binding_b)
    ] == [_hash("token-b")]

    await repositories.watches.create(
        binding_id=binding_a,
        conversation_id=conversation_a,
        pane_id="term-a:0",
        condition_kind="output_contains",
        start_cursor="stream:1:1",
        intent_summary="watch-a",
    )
    await repositories.watches.create(
        binding_id=binding_b,
        conversation_id=conversation_b,
        pane_id="term-b:0",
        condition_kind="output_contains",
        start_cursor="stream:1:1",
        intent_summary="watch-b",
    )
    assert [
        watch.intent_summary
        for watch in await repositories.watches.list_for_binding(binding_a)
    ] == ["watch-a"]
    assert [
        watch.intent_summary
        for watch in await repositories.watches.list_for_binding(binding_b)
    ] == ["watch-b"]

    # Per-conversation sequences are independent: both conversations start at 1.
    item_a = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_a,
        kind="user_message",
        actor_id="actor-a",
        actor_kind="user_session",
        idempotency_key="iso-a-1",
        payload_digest=_hash("a"),
        source="user",
    )
    item_b = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_b,
        kind="user_message",
        actor_id="actor-b",
        actor_kind="user_session",
        idempotency_key="iso-b-1",
        payload_digest=_hash("b"),
        source="user",
    )
    assert item_a.admission_seq == 1
    assert item_b.admission_seq == 1

    # Event cursors and message revisions are per conversation too.
    event_a = await repositories.agent_events.append(
        conversation_id=conversation_a,
        event_kind="run_started",
        dedup_key="iso-evt-a",
        payload_digest=_hash("a"),
    )
    event_b = await repositories.agent_events.append(
        conversation_id=conversation_b,
        event_kind="run_started",
        dedup_key="iso-evt-b",
        payload_digest=_hash("b"),
    )
    assert event_a.database_seq == 1
    assert event_b.database_seq == 1
    message_a = await repositories.agent_messages.create(
        conversation_id=conversation_a,
        role="user",
        kind="text",
        body_digest=_hash("a"),
    )
    message_b = await repositories.agent_messages.create(
        conversation_id=conversation_b,
        role="user",
        kind="text",
        body_digest=_hash("b"),
    )
    assert message_a.assembly_revision == 1
    assert message_b.assembly_revision == 1

    # Cross-scope reads never leak rows from the other Term.
    assert [
        item.id
        for item in await repositories.agent_inbox.list_for_conversation(conversation_a)
    ] == [item_a.id]
    assert [
        event.id
        for event in await repositories.agent_events.list_for_conversation(conversation_a)
    ] == [event_a.id]
    assert [
        message.id
        for message in await repositories.agent_messages.list_for_conversation(
            conversation_a
        )
    ] == [message_a.id]


# ---------------------------------------------------------------------------
# M1 exit gate: fake backend round trip -> durable canonical replay
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal in-test backend: accepts one submission and records it."""

    def __init__(self) -> None:
        self.submitted: list[InboxEnvelope] = []

    async def submit(self, envelope: InboxEnvelope) -> BackendOutcome:
        self.submitted.append(envelope)
        return BackendOutcome.CONFIRMED


def _roundtrip_plan(
    repos: RepositoryBundle,
    conversation_id: UUID,
) -> Callable[[], Awaitable[tuple[FakeBackend, InboxEnvelope]]]:
    """Build the async fake-backend round trip used by the exit-gate test."""

    async def run() -> tuple[FakeBackend, InboxEnvelope]:
        backend = FakeBackend()
        machine = InboxDeliveryStateMachine(repos.agent_inbox, worker_id="exit-gate")
        # B durably accepts a typed user input into the conversation inbox.
        await repos.agent_inbox.enqueue(
            conversation_id=conversation_id,
            kind="user_message",
            actor_id="user-session-1",
            actor_kind="user_session",
            idempotency_key="exit-gate-input",
            payload_digest=_hash("question"),
            source="user",
        )
        envelope = await machine.claim_next(conversation_id=conversation_id)
        assert envelope is not None, "the typed input must be claimable"
        started = await machine.mark_started(
            envelope,
            attempt_id="attempt-1",
            stable_message_id="stable-msg-1",
            owner=envelope.claim_owner,
            fencing_token="fence-1",
        )
        # The fake backend accepts the typed input...
        outcome = await backend.submit(started)
        assert outcome is BackendOutcome.CONFIRMED
        # ...and B records the canonical timeline it produces.
        run_row = await repos.agent_runs.create(
            conversation_id=conversation_id, run_state="running"
        )
        await repos.agent_events.append(
            conversation_id=conversation_id,
            event_kind="run_started",
            dedup_key=f"exit-start-{run_row.id}",
            payload_digest=_hash("start"),
            run_id=run_row.id,
        )
        await repos.agent_events.append(
            conversation_id=conversation_id,
            event_kind="message_completed",
            dedup_key=f"exit-message-{run_row.id}",
            payload_digest=_hash("answer"),
            run_id=run_row.id,
        )
        await repos.agent_events.append(
            conversation_id=conversation_id,
            event_kind="run_completed",
            dedup_key=f"exit-done-{run_row.id}",
            payload_digest=_hash("done"),
            run_id=run_row.id,
        )
        await repos.agent_messages.create(
            conversation_id=conversation_id,
            role="user",
            kind="text",
            body_digest=_hash("question"),
            run_id=run_row.id,
        )
        await repos.agent_messages.create(
            conversation_id=conversation_id,
            role="assistant",
            kind="text",
            body_digest=_hash("answer"),
            run_id=run_row.id,
            is_final=True,
        )
        await repos.agent_runs.set_state(
            run_row.id, "completed", expected_state="running"
        )
        return backend, started

    return run


def test_m1_exit_gate_fake_backend_round_trip_replays_canonical_timeline(
    client,
    admin_headers,
) -> None:
    repos = client.app.state.repositories
    profile = _portal_call(
        client,
        repos.agent_profiles.create,
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = _portal_call(
        client, repos.installations.create, digest_secret(f"computer-{uuid4().hex}")
    )
    display_name = f"term-{uuid4().hex[:8]}"
    term = _portal_call(
        client,
        repos.instances.register_or_rotate,
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    binding = _portal_call(
        client,
        repos.agent_bindings.create,
        profile_id=profile.id,
        term_id=term.id,
        status="pending",
    )
    conversation = _portal_call(
        client,
        repos.agent_conversations.create,
        binding_id=binding.id,
        title="exit-gate",
    )
    backend, started = _portal_call(client, _roundtrip_plan(repos, conversation.id))

    # The typed input was durably admitted and submitted exactly once with
    # its fencing identity intact.
    assert len(backend.submitted) == 1
    submitted = backend.submitted[0]
    assert submitted.stable_message_id == "stable-msg-1"
    assert submitted.fencing_token == "fence-1"
    assert submitted.kind == "user_message"
    assert started.delivery_state == "dispatched"

    # The product APIs replay the canonical timeline.
    events = client.get(
        f"/api/v1/agent/conversations/{conversation.id}/events",
        headers=admin_headers,
    )
    assert events.status_code == 200
    payload = events.json()
    assert [event["event_kind"] for event in payload["events"]] == [
        "run_started",
        "message_completed",
        "run_completed",
    ]
    assert [event["database_seq"] for event in payload["events"]] == [1, 2, 3]
    assert payload["next_cursor"] == 3

    messages = client.get(
        f"/api/v1/agent/conversations/{conversation.id}/messages",
        headers=admin_headers,
    )
    assert messages.status_code == 200
    message_payload = messages.json()
    assert [message["role"] for message in message_payload["messages"]] == [
        "user",
        "assistant",
    ]
    assert [
        message["assembly_revision"] for message in message_payload["messages"]
    ] == [1, 2]
    assert message_payload["messages"][1]["is_final"] is True

    detail = client.get(
        f"/api/v1/agent/conversations/{conversation.id}",
        headers=admin_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["conversation_id"] == str(conversation.id)
