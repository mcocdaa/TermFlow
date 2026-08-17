"""Watch Engine tests (plan §11; task M3.2).

These tests exercise the crash-safe watch engine against a real migrated
database (``Database.initialize`` runs the packaged Alembic chain including
migration ``0007``) wired through the same ``RepositoryBundle`` the agent
repository tests use.

Keeps the most end-to-end paths:

* edge triggering from the creation cursor: a bounded literal split across
  chunks matches exactly once and produces a durable watch delivery plus a
  typed inbox item;
* the engine subscribes to the EventHub and consumes live PANE_OUTPUT wire
  messages into a watch-triggered inbox item.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentInboxItem, Watch
from termflow_control_plane.persistence.repositories import (
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    CursorAcceptance,
    ObservationCursorStore,
    WatchContract,
    WatchEngine,
    encode_watch_contract,
)
from termflow_protocol.common import MessageType, WireMessage
from termflow_protocol.mcp import PaneCursor, WatchCondition, WatchConditionKind
from termflow_protocol.messages import PaneOutputPayload


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


# ---------------------------------------------------------------------------
# WatchEngine: edge triggering from the creation cursor
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

    # The creation cursor's own chunk is consumed before the watch existed:
    # replaying seq 7 cannot retrigger, and the cursor store rejects it as a
    # duplicate.
    replay_triggers = await engine.evaluate_live_event(
        instance_id=binding.term_id,
        pane_id="%0",
        stream_id=stream,
        seq=7,
        data=b"go!",
        observed_at=clock(),
        event_id=uuid4(),
    )
    assert replay_triggers == []
    async with repositories.session_factory() as session:
        acceptance, _ = await store.accept(
            session,
            instance_id=binding.term_id,
            pane_id="%0",
            stream_id=stream,
            seq=7,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.DUPLICATE
        await session.commit()


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


@pytest.mark.asyncio
async def test_engine_consume_survives_invalid_wire_payload(
    repositories: RepositoryBundle,
) -> None:
    """Plan §17: one malformed wire message must not kill the consumer.

    A PANE_OUTPUT whose payload fails validation raises inside
    ``handle_wire_message``; the consume task must record the failure, keep
    running, and still process the next (valid) message into an inbox item.
    """
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
        # Poisoned: PaneOutputPayload.model_validate rejects this dict.
        await hub.publish(
            WireMessage(
                type=MessageType.PANE_OUTPUT,
                instance_id=binding.term_id,
                payload={"pane_id": "%0"},
            )
        )
        # Valid message published after the poison must still be consumed.
        await hub.publish(
            WireMessage(
                type=MessageType.PANE_OUTPUT,
                instance_id=binding.term_id,
                payload=PaneOutputPayload.from_bytes(
                    "%0", stream, 6, b"go!"
                ).model_dump(mode="json"),
            )
        )
        item = await _wait_for_item(repositories, conversation_id)
        assert item is not None
        assert item.kind == "watch_triggered"
        assert engine.diagnostics.wire_messages_failed == 1
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_consume_survives_raising_handler(
    repositories: RepositoryBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan §17: a raising handler (DB error, capture port) must not kill the
    consume task; the message is dropped, recorded, and the loop continues."""
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

    real_handler = engine.handle_wire_message

    async def flaky_handler(message: WireMessage) -> list:
        if message.payload.get("poison") is True:
            raise RuntimeError("handler exploded")
        return await real_handler(message)

    monkeypatch.setattr(engine, "handle_wire_message", flaky_handler)
    await engine.start()
    try:
        await hub.publish(
            WireMessage(
                type=MessageType.PANE_OUTPUT,
                instance_id=binding.term_id,
                payload={"poison": True},
            )
        )
        await hub.publish(
            WireMessage(
                type=MessageType.PANE_OUTPUT,
                instance_id=binding.term_id,
                payload=PaneOutputPayload.from_bytes(
                    "%0", stream, 6, b"go!"
                ).model_dump(mode="json"),
            )
        )
        item = await _wait_for_item(repositories, conversation_id)
        assert item is not None
        assert item.kind == "watch_triggered"
        assert engine.diagnostics.wire_messages_failed == 1
    finally:
        await engine.stop()
