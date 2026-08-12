"""Watch Engine tests (plan §11; task M3.2).

These tests exercise the crash-safe watch engine against a real migrated
database (``Database.initialize`` runs the packaged Alembic chain including
migration ``0007``) wired through the same ``RepositoryBundle`` the agent
repository tests use.

Coverage mirrors the M3.2 scope:

* :class:`LiteralMatcher`: a bounded literal split across chunks matches
  exactly once (edge-triggered), the persisted suffix round-trips through a
  restart, and old scrollback before the creation cursor never matches.
* :class:`IdleDeadline`: silence fires only after an observed cursor plus the
  configured duration and resets on new output; the deadline heap is rebuilt
  from persistent watches at startup.
* :class:`PaneExitDetector` / topology: pane exit fires on the observed
  transition.
* :class:`ObservationCursorStore`: duplicate, out-of-order, seq-gap, and
  epoch-rollback events are rejected; stream changes break continuity.
* :class:`WatchEngine`: edge triggering from the creation cursor, rearm
  advancing generation without retrigger while the condition holds, expiry
  and cancellation racing the trigger transaction, gap reconciliation
  producing an indeterminate observation (live-only watches never match
  snapshot text), restart recovery without re-triggering, and the trigger
  transaction being atomic so a crash between inserts cannot orphan an inbox
  item.
* :func:`build_watch_triggered_input`: typed ``WatchTriggered`` AgentInput
  fields, bounded/redacted observation reference, and a proposed action that
  is explicitly not pre-approved.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import (
    AgentInboxItem,
    ApprovalRequest,
    Watch,
    WatchDelivery,
)
from termflow_control_plane.persistence.repositories import (
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    CursorAcceptance,
    GapReconciliation,
    IdleDeadline,
    LiteralMatcher,
    ObservationCursorStore,
    PaneExitDetector,
    TriggerEvidence,
    WatchContract,
    WatchEngine,
    build_observation_ref,
    build_watch_triggered_input,
    decode_watch_start,
    encode_watch_contract,
    runtime_from_watch_row,
)
from termflow_protocol.agent import (
    AgentActorKind,
    AgentInputSource,
    WatchTriggeredInput,
)
from termflow_protocol.common import MessageType, WireMessage
from termflow_protocol.mcp import PaneCursor, WatchCondition, WatchConditionKind
from termflow_protocol.messages import PaneOutputPayload, TopologyChangedPayload
from termflow_protocol.topology import PaneSnapshot, TopologySnapshot, WindowSnapshot


class Clock:
    """Mutable time source so tests can drive deadlines deterministically."""

    def __init__(self, start: datetime | None = None) -> None:
        self._value = start or datetime.now(UTC)

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


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_binding_and_conversation(repositories: RepositoryBundle):
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repositories.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term_id = uuid4()
    display_name = f"term-{uuid4().hex[:8]}"
    await repositories.instances.register_or_rotate(
        term_id,
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term_id,
        status="active",
    )
    conversation = await repositories.agent_conversations.create(binding_id=binding.id)
    return binding, conversation.id


def _cursor(
    instance_id: UUID,
    pane_id: str = "%0",
    *,
    incarnation: int = 1,
    seq: int = 1,
    stream_id: UUID | None = None,
) -> PaneCursor:
    return PaneCursor(
        instance_id=instance_id,
        pane_id=pane_id,
        pane_incarnation=incarnation,
        stream_id=stream_id or uuid4(),
        seq=seq,
    )


def _condition(kind: WatchConditionKind, **overrides) -> WatchCondition:
    values: dict[str, object] = {"kind": kind}
    if kind is WatchConditionKind.OUTPUT_CONTAINS:
        values["match"] = "deploy complete"
    elif kind is WatchConditionKind.OUTPUT_IDLE:
        values["idle_after_seconds"] = 60
    values.update(overrides)
    return WatchCondition(**values)


async def _seed_watch(
    repositories: RepositoryBundle,
    binding,
    conversation_id: UUID,
    *,
    pane_id: str = "%0",
    condition: WatchCondition,
    start_cursor: PaneCursor | None = None,
    one_shot: bool = True,
    expiry_at: datetime | None = None,
    intent: str = "wait for the deployment to finish",
    proposed_action: str | None = None,
) -> Watch:
    contract = WatchContract(
        condition=condition,
        start_cursor=start_cursor,
        intent_summary=intent,
        proposed_action=proposed_action,
    )
    return await repositories.watches.create(
        binding_id=binding.id,
        conversation_id=conversation_id,
        pane_id=pane_id,
        condition_kind=condition.kind.value,
        start_cursor=encode_watch_contract(contract),
        intent_summary=intent,
        one_shot=one_shot,
        expiry_at=expiry_at,
    )


def _engine(
    repositories: RepositoryBundle,
    *,
    clock: Clock,
    hub: EventHub | None = None,
    capture=None,
) -> WatchEngine:
    return WatchEngine(
        sessions=repositories.session_factory,
        watches=repositories.watches,
        deliveries=repositories.agent_watch_deliveries,
        inbox=repositories.agent_inbox,
        hub=hub,
        clock=clock,
        capture=capture,
    )


async def _inbox_items(repositories: RepositoryBundle, conversation_id: UUID) -> list:
    async with repositories.session_factory() as session:
        rows = await session.scalars(
            select(AgentInboxItem).where(
                AgentInboxItem.conversation_id == conversation_id
            )
        )
        return list(rows)


async def _wait_for_item(
    repositories: RepositoryBundle,
    conversation_id: UUID,
    *,
    timeout_seconds: float = 2.0,
) -> AgentInboxItem | None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        items = await _inbox_items(repositories, conversation_id)
        if items:
            return items[0]
        await asyncio.sleep(0.01)
    return None


def _topology(*pane_ids: str, revision: int = 1, dead: set[str] | None = None) -> TopologySnapshot:
    dead = dead or set()
    panes = [
        PaneSnapshot(
            pane_id=pane_id,
            window_id="@0",
            index=index,
            title="pane",
            width=80,
            height=24,
            active=index == 0,
            dead=pane_id in dead,
        )
        for index, pane_id in enumerate(pane_ids)
    ]
    return TopologySnapshot(
        session_id="$0",
        session_name="test",
        revision=revision,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="win0",
                active=True,
                panes=panes,
            )
        ],
    )


class FakeGapCapture:
    """Test double for the bounded capture reconciliation port."""

    def __init__(self, reconciliation: GapReconciliation | None) -> None:
        self._reconciliation = reconciliation
        self.calls: list[tuple[UUID, str]] = []

    async def reconcile(self, instance_id: UUID, pane_id: str) -> GapReconciliation | None:
        self.calls.append((instance_id, pane_id))
        return self._reconciliation


# ---------------------------------------------------------------------------
# LiteralMatcher
# ---------------------------------------------------------------------------


def test_literal_matcher_matches_literal_split_across_chunks_exactly_once() -> None:
    matcher = LiteralMatcher("deploy complete")
    assert matcher.feed("deploy ") is False
    assert matcher.feed("comple") is False
    assert matcher.feed("te now") is True
    # Edge-triggered: the same literal later cannot re-match within one
    # generation; the engine resets the matcher on rearm.
    assert matcher.feed("deploy complete") is False


def test_literal_matcher_suffix_is_bounded() -> None:
    matcher = LiteralMatcher("abc")
    matcher.feed("x" * 10_000)
    assert len(matcher.suffix) <= len(matcher.needle) - 1
    matcher.feed("ab")  # partial suffix retained for a future chunk
    assert matcher.suffix == "ab"


def test_literal_matcher_state_round_trips_through_restart() -> None:
    matcher = LiteralMatcher("deploy complete")
    matcher.feed("deploy ")
    state = matcher.state()
    restarted = LiteralMatcher.from_state(state)
    assert restarted.suffix == "deploy "
    assert restarted.feed("complete") is True


# ---------------------------------------------------------------------------
# IdleDeadline
# ---------------------------------------------------------------------------


def test_idle_deadline_fires_only_after_cursor_and_duration() -> None:
    t0 = datetime.now(UTC)
    deadline = IdleDeadline(60)
    assert deadline.due(t0 + timedelta(seconds=61)) is False
    deadline.on_output(t0)
    assert deadline.due(t0 + timedelta(seconds=59)) is False
    assert deadline.due(t0 + timedelta(seconds=60)) is True


def test_idle_deadline_resets_on_new_output() -> None:
    t0 = datetime.now(UTC)
    deadline = IdleDeadline(60)
    deadline.on_output(t0)
    deadline.on_output(t0 + timedelta(seconds=30))
    assert deadline.due(t0 + timedelta(seconds=89)) is False
    assert deadline.due(t0 + timedelta(seconds=90)) is True


def test_idle_deadline_state_round_trips() -> None:
    t0 = datetime.now(UTC)
    deadline = IdleDeadline(60)
    deadline.on_output(t0)
    restarted = IdleDeadline.from_state(deadline.state())
    assert restarted.has_cursor is True
    assert restarted.due(t0 + timedelta(seconds=60)) is True


# ---------------------------------------------------------------------------
# PaneExitDetector
# ---------------------------------------------------------------------------


def test_pane_exit_detector_fires_only_on_observed_transition() -> None:
    before = _topology("%0", "%1")
    after = _topology("%1")
    assert PaneExitDetector.exited(before, after, "%0") is True
    assert PaneExitDetector.exited(before, after, "%1") is False
    assert PaneExitDetector.exited(None, after, "%0") is False
    assert PaneExitDetector.exited(after, after, "%0") is False


def test_pane_exit_detector_marks_dead_pane_as_exited() -> None:
    before = _topology("%0")
    after = _topology("%0", dead={"%0"})
    assert PaneExitDetector.exited(before, after, "%0") is True


# ---------------------------------------------------------------------------
# ObservationCursorStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_store_acceptance_rules(repositories: RepositoryBundle) -> None:
    instance_id = uuid4()
    stream = uuid4()
    store = ObservationCursorStore(repositories.session_factory)
    async with repositories.session_factory() as session:
        acceptance, cursor = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.ACCEPTED
        assert cursor.pane_incarnation == 1
        assert cursor.seq == 1

        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.ACCEPTED
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.DUPLICATE
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.OUT_OF_ORDER
        # A seq jump on the same stream is a gap: continuity cannot be proven.
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=4,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.GAP
        # A different stream id breaks continuity (ring restart/overwrite).
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=uuid4(),
            seq=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.STREAM_CHANGED
        await session.commit()


@pytest.mark.asyncio
async def test_cursor_store_incarnation_rollback_rejected(repositories: RepositoryBundle) -> None:
    instance_id = uuid4()
    store = ObservationCursorStore(repositories.session_factory)
    async with repositories.session_factory() as session:
        acceptance, cursor = await store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=uuid4(),
            seq=1,
            pane_incarnation=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.INCARNATION_CHANGED
        assert cursor.pane_incarnation == 2
        acceptance, _ = await store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=uuid4(),
            seq=1,
            pane_incarnation=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.EPOCH_ROLLBACK
        await session.commit()
    got = await store.get_cursor(instance_id, "%0")
    assert got is not None
    assert got.pane_incarnation == 2


# ---------------------------------------------------------------------------
# WatchEngine: edge triggering and cursor rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_edge_triggers_from_creation_cursor(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    event_id = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"starting ",
        observed_at=clock(),
        event_id=event_id,
    )
    assert triggers == []

    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=7,
        data=b"go!",
        observed_at=clock(),
        event_id=event_id,
    )
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.deduped is False
    assert trigger.evidence.source == "live_output"
    assert trigger.evidence.event_id == event_id
    assert trigger.delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|7"
    assert trigger.delivery.inbox_item_id == trigger.inbox_item.id
    assert trigger.delivery.trigger_event_id == event_id
    assert trigger.inbox_item.kind == "watch_triggered"
    assert trigger.inbox_item.actor_kind == "watch_engine"
    assert trigger.inbox_item.source == "system"
    assert trigger.inbox_item.idempotency_key == trigger.delivery.delivery_key

    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "triggered"

    store = ObservationCursorStore(repositories.session_factory)
    got = await store.get_cursor(binding.term_id, "%0")
    assert got is not None and got.seq == 7


@pytest.mark.asyncio
async def test_engine_old_scrollback_cannot_trigger(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    # The creation cursor's own chunk already contained the literal; it was
    # consumed before the watch existed, so it must never fire the watch.
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=5,
        data=b"go!",
        observed_at=clock(),
    )
    assert triggers == []
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"nothing relevant",
        observed_at=clock(),
    )
    assert triggers == []
    store = ObservationCursorStore(repositories.session_factory)
    got = await store.get_cursor(binding.term_id, "%0")
    assert got is not None and got.seq == 6


@pytest.mark.asyncio
async def test_engine_rejects_duplicate_and_out_of_order_events(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    assert (
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
            data=b"go!",
            observed_at=clock(),
        )
    ) != []
    # Duplicate chunk: rejected, no second trigger.
    assert (
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
            data=b"go!",
            observed_at=clock(),
        )
    ) == []
    # Out-of-order chunk: rejected, no trigger, cursor not regressed.
    assert (
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=5,
            data=b"go!",
            observed_at=clock(),
        )
    ) == []
    store = ObservationCursorStore(repositories.session_factory)
    got = await store.get_cursor(binding.term_id, "%0")
    assert got is not None and got.seq == 6


@pytest.mark.asyncio
async def test_engine_v1_creation_envelope_is_decoded(
    repositories: RepositoryBundle,
) -> None:
    """The M2 ``WatchContinuationService`` envelope keeps working."""
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    cursor = _cursor(binding.term_id, seq=5, stream_id=stream)
    v1 = json.dumps(
        {
            "v": 1,
            "condition": {"kind": "output_contains", "match": "go"},
            "cursor": cursor.model_dump(mode="json"),
        },
        separators=(",", ":"),
    )
    watch = await repositories.watches.create(
        binding_id=binding.id,
        conversation_id=conversation_id,
        pane_id="%0",
        condition_kind="output_contains",
        start_cursor=v1,
        intent_summary="legacy watch",
    )
    contract = decode_watch_start(v1)
    assert contract.condition.match == "go"
    assert contract.start_cursor == cursor
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    assert triggers[0].delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|6"


# ---------------------------------------------------------------------------
# WatchEngine: rearm, expiry, cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rearm_advances_generation_and_does_not_retrigger_while_condition_holds(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
        one_shot=False,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    assert triggers[0].delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|6"
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None
    assert row.state == "active"
    assert row.watch_generation == 1
    assert row.rearm_cursor is not None

    # Replaying the same event cannot retrigger.
    assert (
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
            data=b"go!",
            observed_at=clock(),
        )
    ) == []
    # New output that does not contain the literal cannot retrigger.
    assert (
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=7,
            data=b"nothing",
            observed_at=clock(),
        )
    ) == []
    # A new edge after the rearm cursor fires the next generation.
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=8,
        data=b"go again",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    assert triggers[0].delivery.delivery_key == f"{watch.id.hex}|1|1|{stream.hex}|8"


@pytest.mark.asyncio
async def test_expired_watch_never_fires(repositories: RepositoryBundle) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    clock = Clock()
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
        expiry_at=clock() - timedelta(seconds=1),
    )
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    assert triggers == []


@pytest.mark.asyncio
async def test_expiry_race_is_decided_in_trigger_transaction(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
        expiry_at=None,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    runtime = runtime_from_watch_row(watch, binding.term_id)
    # The watch expires between evaluation and the trigger transaction.
    clock.advance(seconds=120)
    await repositories.watches.set_expiry(watch.id, clock())
    evidence = TriggerEvidence(
        event_id=uuid4(),
        source="live_output",
        cursor=_cursor(binding.term_id, seq=6, stream_id=stream),
    )
    trigger = await engine.fire(runtime, evidence, observed_at=clock())
    assert trigger is None
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "expired"
    assert await _inbox_items(repositories, conversation_id) == []


@pytest.mark.asyncio
async def test_cancellation_race_is_decided_in_trigger_transaction(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    runtime = runtime_from_watch_row(watch, binding.term_id)
    # The watch is cancelled between evaluation and the trigger transaction.
    await repositories.watches.cancel(watch.id)
    evidence = TriggerEvidence(
        event_id=uuid4(),
        source="live_output",
        cursor=_cursor(binding.term_id, seq=6, stream_id=stream),
    )
    trigger = await engine.fire(runtime, evidence, observed_at=clock())
    assert trigger is None
    assert await _inbox_items(repositories, conversation_id) == []


# ---------------------------------------------------------------------------
# WatchEngine: delivery idempotency and trigger transaction atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_delivery_key_returns_existing_item_without_double_wake(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
        one_shot=False,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    runtime = runtime_from_watch_row(watch, binding.term_id)
    evidence = TriggerEvidence(
        event_id=uuid4(),
        source="live_output",
        cursor=_cursor(binding.term_id, seq=6, stream_id=stream),
        observation_ref=build_observation_ref(
            source="live_output",
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
        ),
    )
    trigger = await engine.fire(runtime, evidence, observed_at=clock())
    assert trigger is not None and trigger.deduped is False

    # Simulate a replayed/raced trigger carrying the same delivery key: the
    # transaction returns the already-committed inbox item and never inserts
    # a second one.
    trigger = await engine.fire(runtime, evidence, observed_at=clock())
    assert trigger is not None
    assert trigger.deduped is True
    assert trigger.delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|6"
    items = await _inbox_items(repositories, conversation_id)
    assert len(items) == 1
    assert items[0].id == trigger.inbox_item.id


@pytest.mark.asyncio
async def test_trigger_transaction_is_atomic_no_orphaned_inbox_item(
    repositories: RepositoryBundle,
    monkeypatch,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    # Crash between the inbox insert and the delivery insert: the whole event
    # transaction must roll back so no orphaned inbox item exists.
    async def crash_after_inbox_insert(*args, **kwargs) -> None:
        raise RuntimeError("simulated crash between inserts")

    import termflow_control_plane.plugins.agent_broker.agent.watches as watches_module

    monkeypatch.setattr(watches_module, "_insert_watch_delivery", crash_after_inbox_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await engine.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
            data=b"go!",
            observed_at=clock(),
        )

    async with repositories.session_factory() as session:
        deliveries = (await session.scalars(select(WatchDelivery))).all()
        assert deliveries == []
        items = (await session.scalars(select(AgentInboxItem))).all()
        assert items == []
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "active"
    assert row.watch_generation == 0
    store = ObservationCursorStore(repositories.session_factory)
    assert await store.get_cursor(binding.term_id, "%0") is None


@pytest.mark.asyncio
async def test_delivery_failure_records_attempt_count_and_error(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    delivery = triggers[0].delivery
    updated = await engine.record_delivery_attempt(
        delivery.id,
        last_error="backend unreachable",
        next_attempt_at=clock() + timedelta(seconds=30),
    )
    assert updated is not None
    assert updated.attempt_count == 1
    assert updated.last_error == "backend unreachable"
    assert updated.next_attempt_at == clock() + timedelta(seconds=30)


# ---------------------------------------------------------------------------
# WatchEngine: pane exit and topology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pane_exited_fires_on_topology_transition(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        pane_id="%0",
        condition=_condition(WatchConditionKind.PANE_EXITED),
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    event_id = uuid4()

    triggers = await engine.evaluate_topology_change(
        instance_id=binding.term_id,
        topology=_topology("%0", "%1"),
        observed_at=clock(),
        event_id=event_id,
    )
    assert triggers == []
    triggers = await engine.evaluate_topology_change(
        instance_id=binding.term_id,
        topology=_topology("%1"),
        observed_at=clock(),
        event_id=event_id,
    )
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.evidence.source == "topology_exit"
    assert trigger.evidence.event_id == event_id
    assert trigger.delivery.delivery_key == f"{watch.id.hex}|0|0|exit|{event_id.hex}"
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "triggered"


@pytest.mark.asyncio
async def test_pane_exited_handled_from_wire_message(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        pane_id="%0",
        condition=_condition(WatchConditionKind.PANE_EXITED),
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    async def publish_topology(topology: TopologySnapshot, event_id: UUID) -> None:
        message = WireMessage(
            type=MessageType.TOPOLOGY_CHANGED,
            instance_id=binding.term_id,
            payload=TopologyChangedPayload(topology=topology).model_dump(mode="json"),
        )
        await engine.handle_wire_message(message)

    await publish_topology(_topology("%0"), uuid4())
    assert (
        await engine.handle_wire_message(
            WireMessage(
                type=MessageType.TOPOLOGY_CHANGED,
                instance_id=binding.term_id,
                payload=TopologyChangedPayload(topology=_topology("%1")).model_dump(
                    mode="json"
                ),
            )
        )
    ) != []


# ---------------------------------------------------------------------------
# WatchEngine: gaps, indeterminate observations, and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_reconciliation_is_indeterminate_and_never_matches_snapshot(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    old_stream = uuid4()
    new_stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=old_stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    event_id = uuid4()
    snapshot_text = "snapshot text containing the literal go"  # must NOT match
    capture = FakeGapCapture(
        GapReconciliation(
            cursor=_cursor(binding.term_id, seq=10, stream_id=new_stream),
            content=snapshot_text,
            byte_count=len(snapshot_text.encode("utf-8")),
        )
    )
    engine = _engine(repositories, clock=clock, capture=capture)
    await engine.rebuild()

    triggers = await engine.evaluate_gap_event(
        instance_id=binding.term_id,
        pane_id="%0",
        previous_stream_id=old_stream,
        reason="overwritten",
        observed_at=clock(),
        event_id=event_id,
    )
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.evidence.source == "gap_snapshot"
    assert trigger.delivery.delivery_key == f"{trigger.watch.id.hex}|0|1|gap|{event_id.hex}"
    assert trigger.evidence.observation_ref is not None
    assert "gap_snapshot" in trigger.evidence.observation_ref
    assert capture.calls == [(binding.term_id, "%0")]
    # The recovered anchor is persisted so live matching resumes from it.
    store = ObservationCursorStore(repositories.session_factory)
    got = await store.get_cursor(binding.term_id, "%0")
    assert got is not None
    assert got.stream_id == new_stream and got.seq == 10


@pytest.mark.asyncio
async def test_gap_without_capture_port_marks_pane_paused(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    old_stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=old_stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock, capture=None)
    await engine.rebuild()
    triggers = await engine.evaluate_gap_event(
        instance_id=binding.term_id,
        pane_id="%0",
        previous_stream_id=old_stream,
        reason="control_paused",
        observed_at=clock(),
    )
    assert triggers == []
    assert await _inbox_items(repositories, conversation_id) == []


@pytest.mark.asyncio
async def test_idle_deadline_is_paused_while_gap_holds(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_IDLE, idle_after_seconds=60),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock, capture=None)
    await engine.rebuild()
    # Live output arms the idle timer.
    await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"activity",
        observed_at=clock(),
    )
    # A gap pauses the pane; the deadline can no longer fire.
    await engine.evaluate_gap_event(
        instance_id=binding.term_id,
        pane_id="%0",
        previous_stream_id=stream,
        reason="overwritten",
        observed_at=clock(),
    )
    clock.advance(seconds=61)
    assert await engine.check_deadlines(clock()) == []


@pytest.mark.asyncio
async def test_stream_change_on_live_event_enters_gap_reconciliation(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    old_stream = uuid4()
    new_stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=old_stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    capture = FakeGapCapture(
        GapReconciliation(
            cursor=_cursor(binding.term_id, seq=1, stream_id=new_stream),
            content=None,
        )
    )
    engine = _engine(repositories, clock=clock, capture=capture)
    await engine.rebuild()
    # First event on the old stream arms nothing (before the cursor).
    await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=old_stream,
        seq=5,
        data=b"go!",
        observed_at=clock(),
    )
    # A live event on a new stream is a continuity break: reconciliation.
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=new_stream,
        seq=1,
        data=b"go!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    assert triggers[0].evidence.source == "gap_snapshot"


@pytest.mark.asyncio
async def test_incarnation_replacement_invalidates_old_watches(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    old_watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=_cursor(binding.term_id, seq=5, stream_id=stream, incarnation=1),
    )
    new_watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=_cursor(binding.term_id, seq=1, stream_id=stream, incarnation=2),
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    await engine.evaluate_recovered_cursor(
        _cursor(binding.term_id, seq=2, stream_id=stream, incarnation=2),
        observed_at=clock(),
    )
    assert (await repositories.watches.get_by_id(old_watch.id)).state == "cancelled"
    assert (await repositories.watches.get_by_id(new_watch.id)).state == "active"


# ---------------------------------------------------------------------------
# WatchEngine: idle deadlines and restart recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_fires_after_duration_and_resets_on_output(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_IDLE, idle_after_seconds=60),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()

    # No cursor observed yet: silence alone must not fire.
    clock.advance(seconds=61)
    assert await engine.check_deadlines(clock()) == []
    clock.advance(seconds=-61)

    await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"first output",
        observed_at=clock(),
    )
    clock.advance(seconds=59)
    assert await engine.check_deadlines(clock()) == []
    clock.advance(seconds=1)
    triggers = await engine.check_deadlines(clock())
    assert len(triggers) == 1
    assert triggers[0].evidence.source == "idle_deadline"
    assert triggers[0].delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|6"
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "triggered"


@pytest.mark.asyncio
async def test_idle_deadline_heap_is_rebuilt_from_persistent_watches(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_IDLE, idle_after_seconds=60),
        start_cursor=start,
    )
    t0 = datetime.now(UTC)
    clock = Clock(start=t0)
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"output",
        observed_at=clock(),
    )

    # A brand-new engine instance rebuilds its deadline heap from the
    # persisted cursor/matcher state and fires on schedule.
    restarted = _engine(repositories, clock=clock)
    await restarted.rebuild()
    clock.advance(seconds=59)
    assert await restarted.check_deadlines(clock()) == []
    clock.advance(seconds=1)
    triggers = await restarted.check_deadlines(clock())
    assert len(triggers) == 1
    assert triggers[0].evidence.source == "idle_deadline"


@pytest.mark.asyncio
async def test_restart_recovery_resumes_partial_match_without_retrigger(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="X complete"),
        start_cursor=start,
    )
    clock = Clock()
    engine_a = _engine(repositories, clock=clock)
    await engine_a.rebuild()
    # First chunk carries a partial literal; the suffix is persisted.
    assert (
        await engine_a.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=6,
            data=b"X com",
            observed_at=clock(),
        )
    ) == []

    # The process restarts; the new engine resumes from the persisted cursor
    # and matcher suffix, completing the match exactly once.
    engine_b = _engine(repositories, clock=clock)
    await engine_b.rebuild()
    triggers = await engine_b.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=7,
        data=b"plete!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    assert triggers[0].delivery.delivery_key == f"{watch.id.hex}|0|1|{stream.hex}|7"
    # Replaying the same chunk after the restart cannot retrigger.
    assert (
        await engine_b.evaluate_live_event(
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=7,
            data=b"plete!",
            observed_at=clock(),
        )
    ) == []
    # The watch fired once and terminated (one-shot).
    row = await repositories.watches.get_by_id(watch.id)
    assert row is not None and row.state == "triggered"


# ---------------------------------------------------------------------------
# WatchEngine: EventHub subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_subscribes_to_event_hub_and_consumes_live_events(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    hub = EventHub(queue_size=16)
    engine = _engine(repositories, clock=clock, hub=hub)
    await engine.start()
    try:
        message = WireMessage(
            type=MessageType.PANE_OUTPUT,
            instance_id=binding.term_id,
            payload=PaneOutputPayload.from_bytes(
                "%0", stream, 6, b"go!"
            ).model_dump(mode="json"),
        )
        await hub.publish(message)
        item = await _wait_for_item(repositories, conversation_id)
        assert item is not None
        assert item.kind == "watch_triggered"
    finally:
        await engine.stop()


# ---------------------------------------------------------------------------
# Continuation payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_watch_triggered_input_fields_and_bounds(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
        intent="notify when the deploy banner appears",
        proposed_action="read the pane to verify deployment status",
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    assert len(triggers) == 1
    trigger = triggers[0]
    contract = decode_watch_start(watch.start_cursor)
    cursor = _cursor(binding.term_id, seq=6, stream_id=stream)
    observation_ref = build_observation_ref(
        source="live_output",
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        byte_count=3,
        excerpt="go!" * 2000,  # deliberately oversized
    )
    assert len(observation_ref) <= 2048

    sent = build_watch_triggered_input(
        watch=trigger.watch,
        contract=contract,
        delivery_key=trigger.delivery.delivery_key,
        inbox_item=trigger.inbox_item,
        trigger_event_id=trigger.evidence.event_id,
        trigger_source=trigger.evidence.source,
        observed_at=clock(),
        observation_ref=observation_ref,
        cursor=cursor,
        fired_generation=0,
    )
    assert isinstance(sent, WatchTriggeredInput)
    assert sent.kind == "watch_triggered"
    assert sent.conversation_id == conversation_id
    assert sent.actor_kind is AgentActorKind.WATCH_ENGINE
    assert sent.source is AgentInputSource.SYSTEM
    assert sent.idempotency_key == trigger.delivery.delivery_key
    assert sent.admission_seq == trigger.inbox_item.admission_seq
    assert sent.payload.watch_id == watch.id
    assert sent.payload.watch_generation == 0
    assert sent.payload.trigger_event_id == trigger.evidence.event_id
    assert sent.payload.observation_ref == observation_ref
    assert len(sent.payload.observation_ref) <= 2048

    continuation = json.loads(sent.payload.continuation)
    assert continuation["watch_id"] == str(watch.id)
    assert continuation["watch_generation"] == 0
    assert continuation["binding_id"] == str(binding.id)
    assert continuation["conversation_id"] == str(conversation_id)
    assert continuation["instance_id"] == str(binding.term_id)
    assert continuation["pane_id"] == "%0"
    assert continuation["condition"]["kind"] == "output_contains"
    assert continuation["condition"]["match"] == "go"
    assert continuation["start_cursor"]["seq"] == 5
    assert continuation["intent_summary"] == "notify when the deploy banner appears"
    assert continuation["wake_behavior"] == "inspect"
    assert continuation["observation_scope"] == "pane %0 on instance " + str(binding.term_id)
    assert continuation["proposed_action"] == "read the pane to verify deployment status"
    assert continuation["pre_approved"] is False
    assert continuation["one_shot"] is True
    assert continuation["expiry_at"] is None
    assert continuation["trigger_source"] == "live_output"

    # A proposed action in continuation text is NOT a pre-approved grant.
    async with repositories.session_factory() as session:
        approvals = (
            await session.scalars(select(func.count()).select_from(ApprovalRequest))
        ).one()
        assert approvals == 0


@pytest.mark.asyncio
async def test_engine_can_build_input_from_trigger(
    repositories: RepositoryBundle,
) -> None:
    """End-to-end: a fired trigger yields a complete typed AgentInput."""
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    stream = uuid4()
    start = _cursor(binding.term_id, seq=5, stream_id=stream)
    watch = await _seed_watch(
        repositories,
        binding,
        conversation_id,
        condition=_condition(WatchConditionKind.OUTPUT_CONTAINS, match="go"),
        start_cursor=start,
    )
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=6,
        data=b"go!",
        observed_at=clock(),
    )
    trigger = triggers[0]
    sent = engine.build_input(trigger, observed_at=clock())
    assert sent is not None
    assert isinstance(sent, WatchTriggeredInput)
    assert sent.payload.watch_id == watch.id
    assert sent.payload.continuation is not None
    assert json.loads(sent.payload.continuation)["watch_generation"] == 0


# ---------------------------------------------------------------------------
# Wire message dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_ignores_unknown_wire_messages(
    repositories: RepositoryBundle,
) -> None:
    binding, conversation_id = await _seed_binding_and_conversation(repositories)
    clock = Clock()
    engine = _engine(repositories, clock=clock)
    await engine.rebuild()
    message = WireMessage(
        type=MessageType.BRIDGE_HEARTBEAT,
        instance_id=binding.term_id,
        payload={"observed_at": clock().isoformat()},
    )
    assert await engine.handle_wire_message(message) == []
