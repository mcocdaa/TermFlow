"""Agent Inbox delivery state machine tests (plan §7; task M1.4).

These tests exercise :class:`InboxDeliveryStateMachine` against a real
migrated database (``Database.initialize`` runs the packaged Alembic chain
including migration ``0006``) wired through the same ``RepositoryBundle`` the
agent repository tests use.  A mutable ``Clock`` drives the state machine's
time source so claim leases and retry deadlines are deterministic.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
    CancelScope,
    ConcurrencyMode,
    ContextMode,
    ReplayMode,
    RuntimeIsolation,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
    InboxEnvelope,
    InboxStateError,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import BackendOutcome


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


async def _seed_conversation(repos: RepositoryBundle) -> UUID:
    profile = await repos.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repos.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    display_name = f"term-{uuid4().hex[:8]}"
    term = await repos.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    binding = await repos.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="pending",
    )
    conversation = await repos.agent_conversations.create(binding_id=binding.id)
    return conversation.id


async def _enqueue(
    repos: RepositoryBundle,
    conversation_id: UUID,
    *,
    observed: datetime,
    key: str,
) -> UUID:
    item = await repos.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key=key,
        payload_digest=_hash(key),
        source="user",
        now=observed,
    )
    return item.id


def _machine(
    repos: RepositoryBundle,
    *,
    clock: Clock,
    max_attempts: int = 3,
    capabilities: AgentBackendCapabilities | None = None,
    worker_id: str = "worker",
) -> InboxDeliveryStateMachine:
    return InboxDeliveryStateMachine(
        repos.agent_inbox,
        now=clock,
        max_attempts=max_attempts,
        capabilities=capabilities,
        worker_id=worker_id,
    )


# ---------------------------------------------------------------------------
# Delivery transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_to_claimed_to_dispatched_happy_path(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="happy")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None
    assert claimed.id == item_id
    assert claimed.delivery_state == "claimed"
    assert claimed.submission_state == "not_started"
    assert claimed.attempt_count == 0
    assert claimed.claim_owner is not None and claimed.claim_owner.startswith("worker:1")
    assert claimed.claim_expires_at == observed + timedelta(seconds=60)

    dispatched = await machine.mark_dispatched(claimed)
    assert dispatched.delivery_state == "dispatched"
    assert dispatched.claim_owner is None

    rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
    assert [row.delivery_state for row in rows] == ["dispatched"]
    # A dispatched item is never claimable again.
    assert await machine.claim_next(conversation_id=conversation_id) is None


@pytest.mark.asyncio
async def test_mark_retry_sets_next_attempt_at_and_reclaim_after_backoff(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="retry")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id

    retried = await machine.mark_retry(claimed, backoff=timedelta(minutes=5))
    assert retried.delivery_state == "retry_wait"
    assert retried.attempt_count == 1
    assert retried.next_attempt_at == observed + timedelta(minutes=5)

    # Before the retry deadline the item is not claimable.
    assert await machine.claim_next(conversation_id=conversation_id) is None

    clock.advance(minutes=6)
    reclaimed = await machine.claim_next(conversation_id=conversation_id)
    assert reclaimed is not None
    assert reclaimed.id == item_id
    assert reclaimed.delivery_state == "claimed"
    assert reclaimed.attempt_count == 1


@pytest.mark.asyncio
async def test_delivery_unknown_is_never_auto_retried(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    await _enqueue(repositories, conversation_id, observed=observed, key="unknown")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None
    started = await machine.mark_started(
        claimed,
        attempt_id="attempt-1",
        stable_message_id="msg-unknown",
        owner=claimed.claim_owner,
        fencing_token="fence-1",
    )
    unknown = await machine.mark_delivery_unknown(started)
    assert unknown.delivery_state == "delivery_unknown"
    assert unknown.submission_state == "unknown"

    assert (
        InboxDeliveryStateMachine.should_auto_retry(
            submission_state=unknown.submission_state,
            outcome=BackendOutcome.UNKNOWN,
        )
        is False
    )

    # Even after the original claim lease expires, a delivery_unknown item is
    # never returned by claim_next: no silent duplicate action.
    clock.advance(seconds=61)
    assert await machine.claim_next(conversation_id=conversation_id) is None


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="dead")
    machine = _machine(repositories, clock=clock, max_attempts=3)

    current = await machine.claim_next(conversation_id=conversation_id)
    assert current is not None and current.id == item_id
    for expected_attempts in (1, 2):
        current = await machine.mark_retry(current, backoff=timedelta(minutes=1))
        assert current.delivery_state == "retry_wait"
        assert current.attempt_count == expected_attempts
        clock.advance(minutes=2)
        reclaimed = await machine.claim_next(conversation_id=conversation_id)
        assert reclaimed is not None and reclaimed.id == item_id
        current = reclaimed

    # The third failure exhausts the bounded attempt budget and dead-letters.
    dead = await machine.mark_retry(current, backoff=timedelta(minutes=1))
    assert dead.delivery_state == "dead_letter"
    assert dead.attempt_count == 2
    assert await machine.claim_next(conversation_id=conversation_id) is None


@pytest.mark.asyncio
async def test_mark_cancelled_from_claimed(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="cancel")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id
    cancelled = await machine.mark_cancelled(claimed)
    assert cancelled.delivery_state == "cancelled"
    assert await machine.claim_next(conversation_id=conversation_id) is None


# ---------------------------------------------------------------------------
# One-in-flight and CAS claiming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_in_flight_per_conversation(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    first_id = await _enqueue(repositories, conversation_id, observed=observed, key="first")
    second_id = await _enqueue(repositories, conversation_id, observed=observed, key="second")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == first_id

    # A second turn for the same conversation stays queued while one is in flight.
    assert await machine.claim_next(conversation_id=conversation_id) is None

    # A terminal state releases the conversation for the next item.
    await machine.dead_letter(claimed)
    reclaimed = await machine.claim_next(conversation_id=conversation_id)
    assert reclaimed is not None and reclaimed.id == second_id


@pytest.mark.asyncio
async def test_parallel_capabilities_allow_multiple_in_flight(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    first_id = await _enqueue(repositories, conversation_id, observed=observed, key="p-1")
    second_id = await _enqueue(repositories, conversation_id, observed=observed, key="p-2")
    capabilities = AgentBackendCapabilities(
        context_mode=ContextMode.RESUME,
        submit_mode=SubmitMode.IDEMPOTENT,
        cancel_scope=CancelScope.RUN,
        replay_mode=ReplayMode.EVENT,
        concurrency_mode=ConcurrencyMode.PARALLEL,
        tool_call_identity=ToolCallIdentity.BINDING,
        runtime_isolation=RuntimeIsolation.CONVERSATION,
    )
    machine = _machine(repositories, clock=clock, capabilities=capabilities)

    first = await machine.claim_next(conversation_id=conversation_id)
    assert first is not None and first.id == first_id
    second = await machine.claim_next(conversation_id=conversation_id)
    assert second is not None and second.id == second_id


@pytest.mark.asyncio
async def test_concurrent_claim_cas_single_winner(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    await _enqueue(repositories, conversation_id, observed=observed, key="race")
    machine_a = _machine(repositories, clock=clock, worker_id="worker-a")
    machine_b = _machine(repositories, clock=clock, worker_id="worker-b")

    results = await asyncio.gather(
        machine_a.claim_next(conversation_id=conversation_id),
        machine_b.claim_next(conversation_id=conversation_id),
    )
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].delivery_state == "claimed"


# ---------------------------------------------------------------------------
# mark_started submission metadata and fencing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_started_records_submission_metadata_before_dispatch(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="started")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id

    # Fencing: only the lease holder can start the submission.
    with pytest.raises(InboxStateError):
        await machine.mark_started(
            claimed,
            attempt_id="attempt-8",
            stable_message_id="msg-x",
            owner="intruder",
            fencing_token="fence-x",
        )

    started = await machine.mark_started(
        claimed,
        attempt_id="attempt-7",
        stable_message_id="msg-abc",
        owner=claimed.claim_owner,
        fencing_token="fence-1",
    )
    assert started.delivery_state == "dispatched"
    assert started.submission_state == "started"
    assert started.attempt_id == "attempt-7"
    assert started.stable_message_id == "msg-abc"
    assert started.fencing_token == "fence-1"
    assert started.claim_owner is None
    assert started.claim_expires_at is None

    # The dispatch is durable in the repository before any network call.
    rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
    assert [row.delivery_state for row in rows] == ["dispatched"]


# ---------------------------------------------------------------------------
# Recovery of stale claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_stale_expired_started_claim_becomes_delivery_unknown(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="crash")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id
    await machine.mark_started(
        claimed,
        attempt_id="attempt-1",
        stable_message_id="msg-crash",
        owner=claimed.claim_owner,
        fencing_token="fence-1",
    )

    clock.advance(seconds=61)
    recovered = await machine.recover_stale(now=clock())
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "delivery_unknown"

    # Recovery must never re-offer an unknown outcome for delivery.
    assert await machine.claim_next(conversation_id=conversation_id) is None


@pytest.mark.asyncio
async def test_recover_stale_not_started_claim_may_retry(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="nurse")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id
    assert claimed.submission_state == "not_started"

    clock.advance(seconds=61)
    recovered = await machine.recover_stale(now=clock())
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "pending"

    # A provably-not-started claim is safely re-delivered.
    reclaimed = await machine.claim_next(conversation_id=conversation_id)
    assert reclaimed is not None and reclaimed.id == item_id


@pytest.mark.asyncio
async def test_recover_stale_reconciled_started_claim_is_restored(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id = await _enqueue(repositories, conversation_id, observed=observed, key="rec")
    machine = _machine(repositories, clock=clock)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id
    await machine.mark_started(
        claimed,
        attempt_id="attempt-1",
        stable_message_id="msg-rec",
        owner=claimed.claim_owner,
        fencing_token="fence-1",
    )

    clock.advance(seconds=61)
    recovered = await machine.recover_stale(now=clock(), reconciled_ids={item_id})
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "dispatched"
    assert recovered[0].submission_state == "accepted"

    # A reconciled (proven accepted) item is never re-offered for delivery.
    assert await machine.claim_next(conversation_id=conversation_id) is None


# ---------------------------------------------------------------------------
# Auto-retry policy and transition guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("submission_state", "outcome", "expected"),
    [
        # Provably not started: safe to retry regardless of any reported outcome.
        ("not_started", BackendOutcome.UNKNOWN, True),
        # Proven rejection of the stable delivery key: safe to retry.
        ("rejected", BackendOutcome.RETRYABLE, True),
        ("rejected", BackendOutcome.UNSUPPORTED, True),
        # Unknown/context-lost outcomes never trigger a duplicate action.
        ("rejected", BackendOutcome.CONTEXT_LOST, False),
        ("rejected", BackendOutcome.UNKNOWN, False),
        ("started", BackendOutcome.UNKNOWN, False),
        ("started", BackendOutcome.REQUESTED, False),
        ("accepted", BackendOutcome.CONFIRMED, False),
        ("unknown", BackendOutcome.UNKNOWN, False),
    ],
)
def test_should_auto_retry_policy(
    submission_state: str,
    outcome: BackendOutcome,
    expected: bool,
) -> None:
    assert (
        InboxDeliveryStateMachine.should_auto_retry(
            submission_state=submission_state,
            outcome=outcome,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_illegal_transitions_raise(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    await _enqueue(repositories, conversation_id, observed=observed, key="illegal")
    machine = _machine(repositories, clock=clock)

    # A pending item has never been claimed, so it cannot be dispatched.
    pending = InboxEnvelope(
        id=uuid4(),
        conversation_id=conversation_id,
        admission_seq=99,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        auth_epoch=None,
        idempotency_key="illegal",
        payload_digest=_hash("illegal"),
        source="user",
        correlation_id=uuid4(),
        causation_id=None,
        delivery_state="pending",
    )
    with pytest.raises(InboxStateError):
        await machine.mark_dispatched(pending)

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None
    started = await machine.mark_started(
        claimed,
        attempt_id="attempt-1",
        stable_message_id="msg-illegal",
        owner=claimed.claim_owner,
        fencing_token="fence-1",
    )
    unknown = await machine.mark_delivery_unknown(started)
    # delivery_unknown and dispatched are not cancellable through the
    # pending/claimed/retry_wait cancellation path.
    with pytest.raises(InboxStateError):
        await machine.mark_cancelled(unknown)
    with pytest.raises(InboxStateError):
        await machine.mark_retry(unknown, backoff=timedelta(minutes=1))
    with pytest.raises(InboxStateError):
        await machine.dead_letter(unknown)
