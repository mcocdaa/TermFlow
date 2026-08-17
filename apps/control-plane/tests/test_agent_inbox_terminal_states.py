"""Inbox terminal-delivery and submission-metadata tests (plan §7/§17 review fix).

These tests exercise :class:`InboxDeliveryStateMachine` against a real
migrated database (``Database.initialize`` runs the packaged Alembic chain)
wired through the same ``RepositoryBundle`` the agent repository tests use.
A mutable ``Clock`` drives the state machine's time source so claim leases
and submission leases are deterministic.

The review-fix invariants under test:

* a dispatched submission persists its attempt/fencing identity and
  submission lease, so recovery needs no in-memory state;
* ``delivered``/``delivery_unknown``/``cancelled`` are persisted terminal
  delivery states that release the one-in-flight gate, so a conversation can
  run more than one turn;
* restart recovery fences expired started submissions from the database.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
    InboxStateError,
)


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
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'inbox-terminal.db'}")
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
    lease_seconds: int = 60,
) -> InboxDeliveryStateMachine:
    return InboxDeliveryStateMachine(
        repos.agent_inbox,
        now=clock,
        max_attempts=max_attempts,
        capabilities=capabilities,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )


async def _started_item(
    repos: RepositoryBundle,
    conversation_id: UUID,
    *,
    clock: Clock,
    key: str,
    machine: InboxDeliveryStateMachine | None = None,
) -> tuple[UUID, InboxDeliveryStateMachine]:
    """Enqueue + claim + start one item; returns (item_id, machine)."""
    observed = clock()
    item_id = await _enqueue(
        repos, conversation_id, observed=observed, key=key
    )
    machine = machine or _machine(repos, clock=clock)
    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id
    started = await machine.mark_started(
        claimed,
        attempt_id=f"attempt-{key}",
        stable_message_id=key,
        owner=claimed.claim_owner,
        fencing_token=f"fence-{key}",
    )
    assert started.delivery_state == "dispatched"
    return item_id, machine


async def _rows(repos: RepositoryBundle, conversation_id: UUID):
    return await repos.agent_inbox.list_for_conversation(conversation_id)


# ---------------------------------------------------------------------------
# Persisted submission metadata (review fix: no in-memory-only started state)
# ---------------------------------------------------------------------------


async def test_mark_started_persists_submission_metadata(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, _ = await _started_item(
        repositories, conversation_id, clock=clock, key="started"
    )

    rows = await _rows(repositories, conversation_id)
    assert [row.delivery_state for row in rows] == ["dispatched"]
    # Attempt + fencing identity and the submission lease persist on the row.
    assert rows[0].claim_owner == "attempt-started|fence-started"
    persisted_expiry = rows[0].claim_expires_at
    assert persisted_expiry is not None
    # SQLite round-trips datetimes naive; attach UTC for the comparison.
    assert persisted_expiry.replace(tzinfo=UTC) == observed + timedelta(seconds=60)


async def test_mark_started_requires_live_claim_owner(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)
    item_id = await _enqueue(
        repositories, conversation_id, observed=observed, key="fence"
    )
    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == item_id

    # Fencing: only the lease holder can start the submission.
    with pytest.raises(InboxStateError):
        await machine.mark_started(
            claimed,
            attempt_id="attempt-x",
            stable_message_id="msg-x",
            owner="intruder",
            fencing_token="fence-x",
        )


# ---------------------------------------------------------------------------
# Terminal delivery states release the one-in-flight gate (multi-turn)
# ---------------------------------------------------------------------------


async def test_mark_delivered_persists_terminal_and_frees_gate(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    first_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="first"
    )
    second_id = await _enqueue(
        repositories, conversation_id, observed=observed, key="second"
    )

    # While the first turn is dispatched, the second item is never claimed.
    assert await machine.claim_next(conversation_id=conversation_id) is None

    envelope = machine._envelope(
        (await _rows(repositories, conversation_id))[0]
    )
    envelope.delivery_state = "dispatched"
    delivered = await machine.mark_delivered(envelope)
    assert delivered.delivery_state == "delivered"
    assert delivered.submission_state == "accepted"

    rows = await _rows(repositories, conversation_id)
    by_id = {row.id: row for row in rows}
    assert by_id[first_id].delivery_state == "delivered"
    assert by_id[first_id].claim_owner is None

    # The gate is freed: the second turn is now claimable (plan §2.1).
    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == second_id


async def test_delivered_item_is_never_reclaimed(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="only"
    )
    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == item_id
    envelope = machine._envelope(row)
    await machine.mark_delivered(envelope)

    assert await machine.claim_next(conversation_id=conversation_id) is None


async def test_mark_delivered_from_non_dispatched_raises(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)
    item_id = await _enqueue(
        repositories, conversation_id, observed=observed, key="pending"
    )
    pending = machine._envelope((await _rows(repositories, conversation_id))[0])
    assert pending.id == item_id and pending.delivery_state == "pending"

    with pytest.raises(InboxStateError):
        await machine.mark_delivered(pending)


# ---------------------------------------------------------------------------
# Persisted delivery_unknown (review fix: visible recoverable state)
# ---------------------------------------------------------------------------


async def test_mark_delivery_unknown_is_persisted_and_never_reclaimed(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="unknown"
    )
    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == item_id
    envelope = machine._envelope(row)
    unknown = await machine.mark_delivery_unknown(envelope)
    assert unknown.delivery_state == "delivery_unknown"
    assert unknown.submission_state == "unknown"

    rows = await _rows(repositories, conversation_id)
    assert [row.delivery_state for row in rows] == ["delivery_unknown"]

    # Never auto-retried: not claimable now, not claimable after a restart.
    assert await machine.claim_next(conversation_id=conversation_id) is None
    restarted = _machine(repositories, clock=clock)
    assert await restarted.claim_next(conversation_id=conversation_id) is None
    assert await restarted.recover_stale(now=clock()) == []


async def test_recover_stale_fences_expired_started_submission_from_database(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="crash"
    )

    clock.advance(seconds=61)
    # Simulated restart: a fresh machine has no in-memory submission state;
    # recovery must fence from the persisted row alone.
    restarted = _machine(repositories, clock=clock)
    recovered = await restarted.recover_stale(now=clock())
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "delivery_unknown"
    assert recovered[0].submission_state == "unknown"
    assert recovered[0].attempt_id == "attempt-crash"
    assert recovered[0].fencing_token == "fence-crash"
    assert recovered[0].stable_message_id == "crash"

    # The terminal state is durable, not a recovered-envelope artefact.
    rows = await _rows(repositories, conversation_id)
    assert [row.delivery_state for row in rows] == ["delivery_unknown"]
    assert await restarted.claim_next(conversation_id=conversation_id) is None


async def test_recover_stale_reconciled_started_becomes_delivered(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, _ = await _started_item(
        repositories, conversation_id, clock=clock, key="rec"
    )

    clock.advance(seconds=61)
    restarted = _machine(repositories, clock=clock)
    recovered = await restarted.recover_stale(
        now=clock(), reconciled_ids={item_id}
    )
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "delivered"
    assert recovered[0].submission_state == "accepted"

    rows = await _rows(repositories, conversation_id)
    assert [row.delivery_state for row in rows] == ["delivered"]
    assert await restarted.claim_next(conversation_id=conversation_id) is None


async def test_recover_stale_not_started_claim_may_retry(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)
    item_id = await _enqueue(
        repositories, conversation_id, observed=observed, key="nurse"
    )
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


# ---------------------------------------------------------------------------
# Cancellation: dispatched -> cancel_requested -> cancelled|delivery_unknown
# ---------------------------------------------------------------------------


async def test_cancel_requested_keeps_gate_until_cancelled(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    first_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="cancel"
    )
    second_id = await _enqueue(
        repositories, conversation_id, observed=observed, key="after"
    )

    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == first_id
    envelope = machine._envelope(row)
    requested = await machine.mark_cancel_requested(envelope)
    assert requested.delivery_state == "cancel_requested"
    rows = await _rows(repositories, conversation_id)
    assert [r.delivery_state for r in rows] == ["cancel_requested", "pending"]

    # The cancel round trip keeps the item in-flight...
    assert await machine.claim_next(conversation_id=conversation_id) is None

    # ...until the backend cancellation is proven terminal.
    cancelled = await machine.mark_cancelled(requested)
    assert cancelled.delivery_state == "cancelled"
    rows = await _rows(repositories, conversation_id)
    by_id = {r.id: r for r in rows}
    assert by_id[first_id].delivery_state == "cancelled"

    claimed = await machine.claim_next(conversation_id=conversation_id)
    assert claimed is not None and claimed.id == second_id


async def test_cancel_requested_can_become_delivery_unknown(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="cancel-unknown"
    )
    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == item_id
    envelope = machine._envelope(row)
    requested = await machine.mark_cancel_requested(envelope)
    unknown = await machine.mark_delivery_unknown(requested)
    assert unknown.delivery_state == "delivery_unknown"
    rows = await _rows(repositories, conversation_id)
    assert [r.delivery_state for r in rows] == ["delivery_unknown"]
    assert await machine.claim_next(conversation_id=conversation_id) is None


async def test_recover_stale_fences_expired_cancel_requested(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    item_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="cancel-crash"
    )
    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == item_id
    await machine.mark_cancel_requested(machine._envelope(row))

    clock.advance(seconds=61)
    restarted = _machine(repositories, clock=clock)
    recovered = await restarted.recover_stale(now=clock())
    assert [item.id for item in recovered] == [item_id]
    assert recovered[0].delivery_state == "delivery_unknown"
    rows = await _rows(repositories, conversation_id)
    assert [r.delivery_state for r in rows] == ["delivery_unknown"]


# ---------------------------------------------------------------------------
# In-flight counting around terminal states
# ---------------------------------------------------------------------------


async def test_in_flight_count_stops_at_terminal_delivery(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    first_id, machine = await _started_item(
        repositories, conversation_id, clock=clock, key="counted"
    )
    assert await machine._in_flight_count(conversation_id, now=clock()) == 1

    row = (await _rows(repositories, conversation_id))[0]
    assert row.id == first_id
    await machine.mark_delivered(machine._envelope(row))
    assert await machine._in_flight_count(conversation_id, now=clock()) == 0
