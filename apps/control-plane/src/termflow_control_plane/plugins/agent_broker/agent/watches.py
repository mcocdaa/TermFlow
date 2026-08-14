"""Watch Engine and durable continuations (plan §11; task M3.2).

Evaluates durable watch conditions against live Term output and topology,
persisting every accepted pane cursor and incremental matcher state so a
restart resumes exactly where stream continuity guarantees (crash-safe).

Pipeline
========

* :meth:`WatchEngine.evaluate_live_event` accepts a ``PANE_OUTPUT`` chunk
  against the per-pane :class:`ObservationCursorStore` ledger.  Duplicate,
  out-of-order, and gap/stream-changed chunks are rejected (or routed into
  bounded capture reconciliation) before any matcher is fed, and the accepted
  cursor plus each active watcher's matcher suffix are persisted
  transactionally for every consumed event (plan §11.2).
* Watches are edge-triggered from their creation cursor: output at or before
  the creation cursor is old scrollback and can never fire a new watch.
* :meth:`WatchEngine.check_deadlines` fires ``output_idle`` watches whose
  silence duration has elapsed since the last observed cursor; the deadline
  heap is rebuilt from the persisted per-pane cursor at startup, and a gap
  pauses the pane so an idle deadline cannot fire on unverified silence.
* :meth:`WatchEngine.fire` performs the atomic trigger transaction: it locks
  the watch generation/cursor, inserts a unique :class:`WatchDelivery`, the
  causally linked unique ``AgentInboxItem`` (``kind=watch_triggered``,
  ``idempotency_key`` = the delivery key), updates watch state, and commits.
  A duplicate delivery key returns the existing inbox item without waking the
  backend twice; cancellation, expiry, and evaluation race inside the same
  transaction.
* Gap reconciliation (plan §11.2) yields an *indeterminate* observation: a
  ``gap_snapshot`` trigger that wakes the agent only to inspect.  Live-only
  watches never match snapshot text.

The unique delivery key encodes ``(watch_id, watch_generation,
pane_incarnation, stream_id, trigger_seq/event_id)``; rearm advances the
generation and cursor so a condition that stays true cannot immediately
retrigger until a new edge is observed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_protocol.agent import (
    AgentActorKind,
    AgentInputSource,
    WatchTriggeredInput,
    WatchTriggeredPayload,
)
from termflow_protocol.common import MessageType, WireMessage
from termflow_protocol.mcp import PaneCursor, WatchCondition, WatchConditionKind
from termflow_protocol.messages import (
    PaneOutputPayload,
    StreamGapPayload,
    TopologyChangedPayload,
)
from termflow_protocol.topology import TopologySnapshot

from termflow_control_plane.connections.event_hub import EventHub, EventSubscriber
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentInboxItem,
    Instance,
    PaneObservationCursor,
    Watch,
    WatchDelivery,
)
from termflow_control_plane.persistence.repositories import (
    AgentInboxRepository,
    WatchDeliveryRepository,
    WatchRepository,
)

logger = logging.getLogger(__name__)

#: Hard bound on the observation reference carried inside a
#: ``WatchTriggered`` payload (mirrors ``MAX_REF_LENGTH`` in the protocol).
MAX_OBSERVATION_REF_LENGTH = 2048


class CursorAcceptance(StrEnum):
    """Verdict for one candidate live observation cursor (plan §11.2)."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    GAP = "gap"
    STREAM_CHANGED = "stream_changed"
    INCARNATION_CHANGED = "incarnation_changed"
    EPOCH_ROLLBACK = "epoch_rollback"


def _aware(value: datetime | None) -> datetime | None:
    """Normalize SQLite round-tripped datetimes back to aware UTC.

    SQLite stores datetimes without a timezone, so values read from the
    database come back naive; comparisons against aware clock values must
    never mix the two.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class GapReconciliation:
    """Recovered stream anchor plus optional bounded snapshot content."""

    cursor: PaneCursor
    content: str | None = None
    byte_count: int | None = None


#: Bounded capture reconciliation port: given an instance and pane, return
#: the recovered anchor and (optional) snapshot content, or None when the
#: pane cannot be reconciled.
GapCapturePort = Callable[[UUID, str], Awaitable[GapReconciliation | None]]


@dataclass
class LiteralMatcher:
    """Edge-triggered bounded literal matcher with a persisted suffix.

    The matcher keeps only the last ``len(needle) - 1`` characters of input
    (``suffix``), which is exactly the state needed to detect a literal split
    across chunks.  It matches at most once per instance: the engine resets
    the matcher on rearm so a condition that stays true cannot retrigger
    until a new edge is observed.
    """

    needle: str
    _suffix: str = ""
    _matched: bool = False

    @property
    def suffix(self) -> str:
        return self._suffix

    def feed(self, chunk: str) -> bool:
        """Consume one chunk; return True exactly once when the literal matches."""
        if self._matched:
            return False
        window = self._suffix + chunk
        limit = max(len(self.needle) - 1, 0)
        self._suffix = window[-limit:] if limit else ""
        if self.needle in window:
            self._matched = True
            return True
        return False

    def state(self) -> dict[str, object]:
        return {"needle": self.needle, "suffix": self._suffix, "matched": self._matched}

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> LiteralMatcher:
        return cls(
            needle=str(state["needle"]),
            _suffix=str(state.get("suffix", "")),
            _matched=bool(state.get("matched", False)),
        )


@dataclass
class IdleDeadline:
    """Silence deadline that fires only after an observed cursor plus duration."""

    seconds: float
    has_cursor: bool = False
    _last_output: datetime | None = None

    def on_output(self, at: datetime) -> None:
        self.has_cursor = True
        self._last_output = at

    def due(self, now: datetime) -> bool:
        if not self.has_cursor or self._last_output is None:
            return False
        return now - self._last_output >= timedelta(seconds=self.seconds)

    def state(self) -> dict[str, object]:
        return {
            "seconds": self.seconds,
            "has_cursor": self.has_cursor,
            "last_output": self._last_output.isoformat() if self._last_output else None,
        }

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> IdleDeadline:
        deadline = cls(seconds=float(state["seconds"]))
        deadline.has_cursor = bool(state.get("has_cursor", False))
        raw = state.get("last_output")
        if raw is not None:
            deadline._last_output = datetime.fromisoformat(str(raw))
        return deadline


class PaneExitDetector:
    """Topology transition detection for ``pane_exited`` watches."""

    @staticmethod
    def exited(
        before: TopologySnapshot | None,
        after: TopologySnapshot,
        pane_id: str,
    ) -> bool:
        if before is None:
            return False
        before_panes = {
            pane.pane_id: pane for window in before.windows for pane in window.panes
        }
        after_panes = {
            pane.pane_id: pane for window in after.windows for pane in window.panes
        }
        before_pane = before_panes.get(pane_id)
        if before_pane is None:
            return False
        after_pane = after_panes.get(pane_id)
        if after_pane is None:
            return True
        return not before_pane.dead and after_pane.dead


@dataclass(frozen=True)
class TriggerEvidence:
    """The observed event that caused a watch to fire."""

    event_id: UUID
    source: str
    cursor: PaneCursor | None = None
    observation_ref: str | None = None


@dataclass(frozen=True)
class WatchContract:
    """Decoded continuation condition stored on a watch row."""

    condition: WatchCondition
    start_cursor: PaneCursor | None = None
    intent_summary: str = ""
    proposed_action: str | None = None


@dataclass
class WatcherRuntime:
    """In-memory evaluation state for one active watch."""

    watch_id: UUID
    binding_id: UUID
    conversation_id: UUID
    instance_id: UUID
    pane_id: str
    condition: WatchCondition
    start_cursor: PaneCursor | None
    intent_summary: str
    proposed_action: str | None
    generation: int
    rearm_cursor: str | None
    one_shot: bool
    expiry_at: datetime | None
    matcher: LiteralMatcher | None
    deadline: IdleDeadline | None
    last_cursor: PaneCursor | None = None


@dataclass(frozen=True)
class FiredTrigger:
    """One committed trigger: the delivery receipt plus its inbox item."""

    watch: Watch
    evidence: TriggerEvidence
    delivery: WatchDelivery
    inbox_item: AgentInboxItem | None
    deduped: bool = False


@dataclass(frozen=True)
class _CursorState:
    """Normalized per-pane ledger state (DB row or in-memory fallback)."""

    incarnation: int
    stream_id: str
    seq: int
    observed_at: datetime


class ObservationCursorStore:
    """Per-pane accepted live observation cursor ledger (plan §11.2).

    The pane-scoped key is ``(instance_id, pane_id)`` because tmux pane IDs
    can repeat across Term instances.  Acceptance runs inside the caller's
    session so the cursor persists transactionally with the event's trigger:
    a rollback of the trigger transaction must roll the accepted cursor back
    with it.

    The ledger row carries a foreign key to ``instances``.  A pane whose
    owning instance is not present in the ``instances`` table (for example a
    unit-level ledger test) keeps its acceptance state in a bounded
    in-memory fallback keyed by the same ``(instance_id, pane_id)`` pair so
    the acceptance rules stay identical without violating referential
    integrity.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._ledger: dict[tuple[UUID, str], _CursorState] = {}

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]

    async def accept(
        self,
        session: AsyncSession,
        *,
        instance_id: UUID,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        observed_at: datetime,
    ) -> tuple[CursorAcceptance, PaneCursor]:
        """Accept or reject one live observation cursor (no commit)."""
        row = await session.get(PaneObservationCursor, (instance_id, pane_id))
        if row is None:
            state = self._ledger.get((instance_id, pane_id))
            if state is None:
                # First observation of this pane.
                await self._create_anchor(
                    session,
                    instance_id=instance_id,
                    pane_id=pane_id,
                    stream_id=stream_id,
                    seq=seq,
                    incarnation=1,
                    observed_at=observed_at,
                )
                return (
                    CursorAcceptance.ACCEPTED,
                    PaneCursor(
                        instance_id=instance_id,
                        pane_id=pane_id,
                        pane_incarnation=1,
                        stream_id=stream_id,
                        seq=seq,
                    ),
                )
        else:
            state = _CursorState(
                int(row.pane_incarnation),
                row.stream_id,
                row.seq,
                self._aware(row.observed_at),
            )
        verdict, accepted = self._evaluate(state, stream_id, seq)
        if accepted:
            if row is not None:
                row.seq = seq
                row.observed_at = observed_at
            else:
                self._ledger[(instance_id, pane_id)] = _CursorState(
                    state.incarnation, str(stream_id), seq, observed_at
                )
            # The accepted cursor reflects the new position.
            return (
                verdict,
                PaneCursor(
                    instance_id=instance_id,
                    pane_id=pane_id,
                    pane_incarnation=state.incarnation,
                    stream_id=stream_id,
                    seq=seq,
                ),
            )
        return verdict, self._cursor_from_state(instance_id, pane_id, state)

    async def accept_recovered(
        self,
        session: AsyncSession,
        *,
        instance_id: UUID,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        pane_incarnation: int,
        observed_at: datetime,
    ) -> tuple[CursorAcceptance, PaneCursor]:
        """Accept a recovered anchor (gap capture / restart replay) (no commit).

        A pane incarnation newer than the ledger is accepted as the new
        anchor and reported as :attr:`CursorAcceptance.INCARNATION_CHANGED`
        so the caller can invalidate watches pinned to an older incarnation.
        A cursor from an older incarnation is an epoch rollback and is
        rejected.
        """
        row = await session.get(PaneObservationCursor, (instance_id, pane_id))
        entry = self._ledger.get((instance_id, pane_id))
        if row is not None:
            current_incarnation = int(row.pane_incarnation)
        else:
            current_incarnation = entry.incarnation if entry is not None else 1
        cursor = PaneCursor(
            instance_id=instance_id,
            pane_id=pane_id,
            pane_incarnation=pane_incarnation,
            stream_id=stream_id,
            seq=seq,
        )
        if pane_incarnation > current_incarnation:
            await self._persist_recovered(
                session,
                row,
                instance_id=instance_id,
                pane_id=pane_id,
                stream_id=stream_id,
                seq=seq,
                pane_incarnation=pane_incarnation,
                observed_at=observed_at,
            )
            return CursorAcceptance.INCARNATION_CHANGED, cursor
        if pane_incarnation < current_incarnation:
            return CursorAcceptance.EPOCH_ROLLBACK, cursor
        await self._persist_recovered(
            session,
            row,
            instance_id=instance_id,
            pane_id=pane_id,
            stream_id=stream_id,
            seq=seq,
            pane_incarnation=pane_incarnation,
            observed_at=observed_at,
        )
        return CursorAcceptance.ACCEPTED, cursor

    async def _create_anchor(
        self,
        session: AsyncSession,
        *,
        instance_id: UUID,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        incarnation: int,
        observed_at: datetime,
    ) -> None:
        """Persist a fresh anchor: the DB row for known instances, otherwise
        the in-memory fallback ledger."""
        if await session.get(Instance, instance_id) is None:
            self._ledger[(instance_id, pane_id)] = _CursorState(
                incarnation, str(stream_id), seq, observed_at
            )
            return
        session.add(
            PaneObservationCursor(
                instance_id=instance_id,
                pane_id=pane_id,
                pane_incarnation=str(incarnation),
                stream_id=str(stream_id),
                seq=seq,
                observed_at=observed_at,
            )
        )

    async def _persist_recovered(
        self,
        session: AsyncSession,
        row: PaneObservationCursor | None,
        *,
        instance_id: UUID,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        pane_incarnation: int,
        observed_at: datetime,
    ) -> None:
        if row is not None:
            row.pane_incarnation = str(pane_incarnation)
            row.stream_id = str(stream_id)
            row.seq = seq
            row.observed_at = observed_at
            return
        if (instance_id, pane_id) in self._ledger:
            self._ledger[(instance_id, pane_id)] = _CursorState(
                pane_incarnation, str(stream_id), seq, observed_at
            )
            return
        await self._create_anchor(
            session,
            instance_id=instance_id,
            pane_id=pane_id,
            stream_id=stream_id,
            seq=seq,
            incarnation=pane_incarnation,
            observed_at=observed_at,
        )

    @staticmethod
    def _evaluate(
        state: _CursorState,
        stream_id: UUID,
        seq: int,
    ) -> tuple[CursorAcceptance, bool]:
        if str(stream_id) != state.stream_id:
            return CursorAcceptance.STREAM_CHANGED, False
        if seq == state.seq:
            return CursorAcceptance.DUPLICATE, False
        if seq < state.seq:
            return CursorAcceptance.OUT_OF_ORDER, False
        if seq != state.seq + 1:
            return CursorAcceptance.GAP, False
        return CursorAcceptance.ACCEPTED, True

    async def get_cursor(self, instance_id: UUID, pane_id: str) -> PaneCursor | None:
        async with self._sessions() as session:
            return await self._get_cursor_in_session(session, instance_id, pane_id)

    async def _get_cursor_in_session(
        self,
        session: AsyncSession,
        instance_id: UUID,
        pane_id: str,
    ) -> PaneCursor | None:
        row = await session.get(PaneObservationCursor, (instance_id, pane_id))
        if row is not None:
            return self._to_cursor(row)
        entry = self._ledger.get((instance_id, pane_id))
        if entry is None:
            return None
        return self._cursor_from_state(instance_id, pane_id, entry)

    async def _get_anchor_in_session(
        self,
        session: AsyncSession,
        instance_id: UUID,
        pane_id: str,
    ) -> tuple[PaneCursor, datetime] | None:
        row = await session.get(PaneObservationCursor, (instance_id, pane_id))
        if row is not None:
            return self._to_cursor(row), self._aware(row.observed_at)
        entry = self._ledger.get((instance_id, pane_id))
        if entry is None:
            return None
        return self._cursor_from_state(instance_id, pane_id, entry), entry.observed_at

    @staticmethod
    def _to_cursor(row: PaneObservationCursor) -> PaneCursor:
        return PaneCursor(
            instance_id=row.instance_id,
            pane_id=row.pane_id,
            pane_incarnation=int(row.pane_incarnation),
            stream_id=UUID(row.stream_id),
            seq=row.seq,
        )

    @classmethod
    def _cursor_from_state(
        cls,
        instance_id: UUID,
        pane_id: str,
        state: _CursorState,
    ) -> PaneCursor:
        return PaneCursor(
            instance_id=instance_id,
            pane_id=pane_id,
            pane_incarnation=state.incarnation,
            stream_id=UUID(state.stream_id),
            seq=state.seq,
        )


def build_delivery_key(
    watch_id: UUID,
    generation: int,
    incarnation: int,
    stream_id: UUID | str,
    seq: int | str,
) -> str:
    """Build the idempotent trigger receipt key (plan §11.2).

    Encodes ``(watch_id, watch_generation, pane_incarnation, stream_id,
    trigger_seq/event_id)``.  ``stream_id`` is the UUID hex for live output
    and the literal ``"exit"``/``"gap"`` markers for topology/gap triggers.
    """
    stream_part = stream_id.hex if isinstance(stream_id, UUID) else str(stream_id)
    return f"{watch_id.hex}|{generation}|{incarnation}|{stream_part}|{seq}"


def build_observation_ref(
    *,
    source: str,
    instance_id: UUID,
    pane_id: str,
    stream_id: UUID | None = None,
    seq: int | None = None,
    byte_count: int | None = None,
    excerpt: str | None = None,
) -> str:
    """Build a bounded, redacted observation reference (max 2048 chars).

    The reference identifies where the observation came from without carrying
    an unbounded terminal transcript: the excerpt is truncated to fit the
    bound.
    """
    envelope: dict[str, object] = {
        "source": source,
        "instance_id": str(instance_id),
        "pane_id": pane_id,
    }
    if stream_id is not None:
        envelope["stream_id"] = str(stream_id)
    if seq is not None:
        envelope["seq"] = seq
    if byte_count is not None:
        envelope["byte_count"] = byte_count
    if excerpt is None:
        return json.dumps(envelope, separators=(",", ":"))
    # Truncate the excerpt so the complete reference fits the hard bound.
    probe = dict(envelope)
    probe["excerpt"] = ""
    budget = max(MAX_OBSERVATION_REF_LENGTH - len(json.dumps(probe, separators=(",", ":"))), 0)
    envelope["excerpt"] = excerpt[:budget]
    return json.dumps(envelope, separators=(",", ":"))


def encode_watch_contract(contract: WatchContract) -> str:
    """Encode a continuation contract into the ``watches.start_cursor`` column."""
    return json.dumps(
        {
            "v": 2,
            "condition": contract.condition.model_dump(mode="json"),
            "start_cursor": (
                contract.start_cursor.model_dump(mode="json")
                if contract.start_cursor is not None
                else None
            ),
            "intent_summary": contract.intent_summary,
            "proposed_action": contract.proposed_action,
        },
        separators=(",", ":"),
    )


def decode_watch_start(encoded: str) -> WatchContract:
    """Decode a persisted ``start_cursor`` envelope (v1 or v2).

    The M2 ``WatchContinuationService`` wrote v1 envelopes carrying
    ``condition`` and ``cursor``; v2 adds ``intent_summary`` and
    ``proposed_action``.  Both stay readable.
    """
    envelope = json.loads(encoded)
    version = envelope.get("v", 1)
    if version == 1:
        return WatchContract(
            condition=WatchCondition.model_validate(envelope["condition"]),
            start_cursor=(
                PaneCursor.model_validate(envelope["cursor"])
                if envelope.get("cursor") is not None
                else None
            ),
            intent_summary="",
            proposed_action=None,
        )
    if version == 2:
        return WatchContract(
            condition=WatchCondition.model_validate(envelope["condition"]),
            start_cursor=(
                PaneCursor.model_validate(envelope["start_cursor"])
                if envelope.get("start_cursor") is not None
                else None
            ),
            intent_summary=str(envelope.get("intent_summary", "")),
            proposed_action=envelope.get("proposed_action"),
        )
    raise ValueError(f"unsupported watch contract version: {version!r}")


def runtime_from_watch_row(watch: Watch, term_id: UUID) -> WatcherRuntime:
    """Build the in-memory watcher runtime from a persisted watch row."""
    contract = decode_watch_start(watch.start_cursor)
    matcher: LiteralMatcher | None = None
    deadline: IdleDeadline | None = None
    if contract.condition.kind is WatchConditionKind.OUTPUT_CONTAINS:
        if watch.matcher_state:
            matcher = LiteralMatcher.from_state(json.loads(watch.matcher_state))
        else:
            matcher = LiteralMatcher(contract.condition.match or "")
    elif contract.condition.kind is WatchConditionKind.OUTPUT_IDLE:
        deadline = IdleDeadline(contract.condition.idle_after_seconds or 1)
    return WatcherRuntime(
        watch_id=watch.id,
        binding_id=watch.binding_id,
        conversation_id=watch.conversation_id,
        instance_id=term_id,
        pane_id=watch.pane_id,
        condition=contract.condition,
        start_cursor=contract.start_cursor,
        intent_summary=contract.intent_summary,
        proposed_action=contract.proposed_action,
        generation=watch.watch_generation,
        rearm_cursor=watch.rearm_cursor,
        one_shot=watch.one_shot,
        expiry_at=watch.expiry_at,
        matcher=matcher,
        deadline=deadline,
    )


def build_watch_triggered_input(
    *,
    watch: Watch,
    contract: WatchContract,
    delivery_key: str,
    inbox_item: AgentInboxItem,
    trigger_event_id: UUID,
    trigger_source: str,
    observed_at: datetime,
    observation_ref: str | None,
    cursor: PaneCursor,
    fired_generation: int,
) -> WatchTriggeredInput:
    """Build the typed ``WatchTriggered`` AgentInput (plan §11.3).

    The continuation carries the condition, start cursor, intent, wake
    behavior, observation scope, and proposed next action.  A proposed
    action in continuation text is NOT a pre-approved grant
    (``pre_approved`` is always False).
    """
    if inbox_item is None:
        raise ValueError("a watch trigger requires its inbox item")
    if cursor is None:
        raise ValueError("a watch trigger requires the observation cursor")
    continuation = json.dumps(
        {
            "watch_id": str(watch.id),
            "watch_generation": fired_generation,
            "binding_id": str(watch.binding_id),
            "conversation_id": str(watch.conversation_id),
            "instance_id": str(cursor.instance_id),
            "pane_id": watch.pane_id,
            "condition": contract.condition.model_dump(mode="json"),
            "start_cursor": (
                contract.start_cursor.model_dump(mode="json")
                if contract.start_cursor is not None
                else None
            ),
            "intent_summary": contract.intent_summary,
            # §11.1: first-release conditions wake the agent to inspect; a
            # marker emitted by a pane process cannot grant authority or
            # prove success.
            "wake_behavior": "inspect",
            "observation_scope": f"pane {watch.pane_id} on instance {cursor.instance_id}",
            "proposed_action": contract.proposed_action,
            "pre_approved": False,
            "one_shot": watch.one_shot,
            "expiry_at": watch.expiry_at.isoformat() if watch.expiry_at is not None else None,
            "trigger_source": trigger_source,
        },
        separators=(",", ":"),
    )
    payload = WatchTriggeredPayload(
        watch_id=watch.id,
        watch_generation=fired_generation,
        trigger_event_id=trigger_event_id,
        continuation=continuation,
        observation_ref=observation_ref,
    )
    return WatchTriggeredInput(
        kind="watch_triggered",
        conversation_id=watch.conversation_id,
        actor_id=str(watch.id),
        actor_kind=AgentActorKind.WATCH_ENGINE,
        admission_seq=inbox_item.admission_seq,
        idempotency_key=delivery_key,
        source=AgentInputSource.SYSTEM,
        causation_id=str(watch.id),
        correlation_id=str(trigger_event_id),
        created_at=observed_at,
        payload=payload,
    )


async def _insert_inbox_item(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    kind: str,
    actor_id: str,
    actor_kind: str,
    idempotency_key: str,
    payload_digest: str,
    source: str,
    auth_epoch: int | None = None,
    causation_id: UUID | None = None,
    correlation_id: UUID | None = None,
    now: datetime | None = None,
) -> AgentInboxItem:
    """Insert one inbox item inside the caller's transaction (no commit).

    Mirrors :meth:`AgentInboxRepository.enqueue` so the B-assigned
    ``admission_seq`` is computed atomically inside the same statement, but
    keeps the insert inside the trigger transaction so a crash between the
    inbox insert and the delivery insert rolls both back.
    """
    observed_at = now or datetime.now(UTC)
    effective_correlation_id = correlation_id or uuid4()
    result = await session.execute(
        insert(AgentInboxItem)
        .from_select(
            [
                AgentInboxItem.conversation_id,
                AgentInboxItem.kind,
                AgentInboxItem.actor_id,
                AgentInboxItem.actor_kind,
                AgentInboxItem.auth_epoch,
                AgentInboxItem.admission_seq,
                AgentInboxItem.idempotency_key,
                AgentInboxItem.payload_digest,
                AgentInboxItem.source,
                AgentInboxItem.delivery_state,
                AgentInboxItem.attempt_count,
                AgentInboxItem.claim_owner,
                AgentInboxItem.claim_expires_at,
                AgentInboxItem.next_attempt_at,
                AgentInboxItem.causation_id,
                AgentInboxItem.correlation_id,
                AgentInboxItem.created_at,
            ],
            select(
                literal(conversation_id),
                literal(kind),
                literal(actor_id),
                literal(actor_kind),
                literal(auth_epoch),
                func.coalesce(func.max(AgentInboxItem.admission_seq), 0) + 1,
                literal(idempotency_key),
                literal(payload_digest),
                literal(source),
                literal("pending"),
                literal(0),
                literal(None),
                literal(None),
                literal(observed_at),
                literal(causation_id),
                literal(effective_correlation_id),
                literal(observed_at),
            ).where(AgentInboxItem.conversation_id == conversation_id),
        )
        .returning(AgentInboxItem)
    )
    return result.scalar_one()


async def _insert_watch_delivery(
    session: AsyncSession,
    *,
    watch_id: UUID,
    delivery_key: str,
    trigger_event_id: UUID | None = None,
    inbox_item_id: UUID | None = None,
) -> WatchDelivery:
    """Insert one delivery receipt inside the caller's transaction (no commit).

    The unique ``delivery_key`` makes the insert idempotent: a raced
    duplicate key returns the already-persisted receipt.  Uses the SQLite
    ``ON CONFLICT DO NOTHING`` form because every control-plane deployment
    is SQLite-backed.
    """
    result = await session.execute(
        sqlite_insert(WatchDelivery)
        .values(
            watch_id=watch_id,
            delivery_key=delivery_key,
            trigger_event_id=trigger_event_id,
            inbox_item_id=inbox_item_id,
        )
        .on_conflict_do_nothing(index_elements=[WatchDelivery.delivery_key])
        .returning(WatchDelivery)
    )
    delivery = result.scalar_one_or_none()
    if delivery is not None:
        return delivery
    existing: WatchDelivery | None = await session.scalar(
        select(WatchDelivery).where(WatchDelivery.delivery_key == delivery_key)
    )
    if existing is None:
        raise RuntimeError("watch delivery insert lost the delivery key")
    return existing


class WatchEngine:
    """Crash-safe evaluation of durable watch conditions (plan §11).

    ``sessions`` is the async session factory, ``watches``/``deliveries``/
    ``inbox`` the watch, delivery, and inbox repositories, ``hub`` the
    optional :class:`EventHub` to subscribe to, ``clock`` the time source,
    and ``capture`` the optional bounded capture reconciliation port.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        watches: WatchRepository,
        deliveries: WatchDeliveryRepository,
        inbox: AgentInboxRepository,
        hub: EventHub | None = None,
        clock: Callable[[], datetime] | None = None,
        capture: GapCapturePort | None = None,
        on_fired: Callable[[FiredTrigger], Awaitable[None]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._watch_repo = watches
        self._delivery_repo = deliveries
        self._inbox_repo = inbox
        self._hub = hub
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capture = capture
        # Trigger sink (review fix M1): every fired trigger - live output,
        # deadline, topology, or gap snapshot - is delivered through this
        # port so the binding's pipeline can register the typed
        # WatchTriggeredInput after ``fire()`` committed the inbox row.
        # ``None`` (unit tests) means triggers are only returned.
        self.on_fired = on_fired
        self._cursor_store = ObservationCursorStore(sessions)
        #: watch_id -> active watcher runtime (rebuilt from the repository).
        self._watches: dict[UUID, WatcherRuntime] = {}
        #: (instance_id, pane_id) panes whose continuity is unproven; idle
        #: deadlines cannot fire while a pane is paused (plan §11.2).
        self._paused_panes: set[tuple[UUID, str]] = set()
        #: instance_id -> last observed topology, for pane-exit detection.
        self._topologies: dict[UUID, TopologySnapshot] = {}
        self._subscriber: EventSubscriber | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def rebuild(self) -> None:
        """Reload active watches and rebuild matchers, deadlines, and anchors."""
        rows = await self._watch_repo.list_active(now=self._clock())
        runtimes: dict[UUID, WatcherRuntime] = {}
        async with self._sessions() as session:
            for row in rows:
                binding = await session.get(AgentBinding, row.binding_id)
                if binding is None:
                    continue
                runtime = runtime_from_watch_row(row, binding.term_id)
                anchor = await self._cursor_store._get_anchor_in_session(
                    session, binding.term_id, row.pane_id
                )
                if anchor is not None:
                    cursor, observed_at = anchor
                    runtime.last_cursor = cursor
                    if runtime.deadline is not None:
                        # Idle timing uses B receipt time: the persisted
                        # cursor's observed_at restarts the timer (plan §11.2).
                        runtime.deadline.on_output(observed_at)
                runtimes[row.id] = runtime
        self._watches = runtimes

    async def start(self) -> None:
        """Load active watches, subscribe to the EventHub, and consume wire
        messages in the background."""
        if self._hub is None or self._task is not None:
            return
        await self.rebuild()
        self._subscriber = await self._hub.subscribe(None)
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        assert self._subscriber is not None
        try:
            while True:
                message = await self._subscriber.queue.get()
                await self.handle_wire_message(message)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Cancel the background consumer and unsubscribe from the EventHub."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._subscriber is not None and self._hub is not None:
            await self._hub.unsubscribe(self._subscriber)
            self._subscriber = None

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    async def evaluate_live_event(
        self,
        instance_id: UUID,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        data: bytes,
        observed_at: datetime,
        event_id: UUID | None = None,
    ) -> list[FiredTrigger]:
        """Evaluate one live ``PANE_OUTPUT`` chunk (plan §11.2).

        The cursor acceptance, matcher suffix persistence, and any trigger
        transaction share one session so a crash between inserts cannot
        orphan an inbox item.
        """
        async with self._sessions() as session:
            acceptance, cursor = await self._cursor_store.accept(
                session,
                instance_id=instance_id,
                pane_id=pane_id,
                stream_id=stream_id,
                seq=seq,
                observed_at=observed_at,
            )
            if acceptance in (CursorAcceptance.STREAM_CHANGED, CursorAcceptance.GAP):
                return await self._reconcile_gap_in_session(
                    session,
                    instance_id=instance_id,
                    pane_id=pane_id,
                    previous_stream_id=stream_id,
                    reason=(
                        "stream_changed"
                        if acceptance is CursorAcceptance.STREAM_CHANGED
                        else "gap"
                    ),
                    observed_at=observed_at,
                    event_id=event_id,
                )
            if acceptance is not CursorAcceptance.ACCEPTED:
                # Duplicate or out-of-order chunk: rejected, cursor not moved.
                return []
            self._paused_panes.discard((instance_id, pane_id))
            text = data.decode("utf-8", errors="replace")
            triggers: list[FiredTrigger] = []
            fired_pairs: list[tuple[WatcherRuntime, FiredTrigger]] = []
            fired_ids: set[UUID] = set()
            for runtime in self._runtimes_for(instance_id, pane_id):
                runtime.last_cursor = cursor
                if not self._consumes(runtime, cursor):
                    # Output at or before the creation cursor was consumed
                    # before the watch existed; it must never fire it.
                    continue
                if runtime.condition.kind is WatchConditionKind.OUTPUT_IDLE:
                    if runtime.deadline is not None:
                        runtime.deadline.on_output(observed_at)
                    continue
                if runtime.condition.kind is WatchConditionKind.OUTPUT_CONTAINS:
                    if runtime.matcher is None:
                        continue
                    if runtime.matcher.feed(text):
                        evidence = TriggerEvidence(
                            event_id=event_id or uuid4(),
                            source="live_output",
                            cursor=cursor,
                            observation_ref=build_observation_ref(
                                source="live_output",
                                instance_id=instance_id,
                                pane_id=pane_id,
                                stream_id=stream_id,
                                seq=seq,
                                byte_count=len(data),
                                excerpt=text,
                            ),
                        )
                        fired = await self._fire_in_session(
                            session, runtime, evidence, observed_at
                        )
                        if fired is not None:
                            triggers.append(fired)
                            fired_pairs.append((runtime, fired))
                            fired_ids.add(runtime.watch_id)
            # Persist each active watcher's matcher suffix transactionally for
            # every consumed live event so a partial match survives a restart.
            for runtime in self._runtimes_for(instance_id, pane_id):
                if runtime.matcher is None or runtime.watch_id in fired_ids:
                    continue
                row = await session.get(Watch, runtime.watch_id)
                if row is not None and row.state == "active":
                    row.matcher_state = json.dumps(
                        runtime.matcher.state(), separators=(",", ":")
                    )
            await session.commit()
        for runtime, fired in fired_pairs:
            self._apply_trigger_state(runtime, fired)
        await self._deliver(triggers)
        return triggers

    async def evaluate_gap_event(
        self,
        instance_id: UUID,
        pane_id: str,
        previous_stream_id: UUID,
        reason: str,
        observed_at: datetime,
        event_id: UUID | None = None,
    ) -> list[FiredTrigger]:
        """Handle an explicit ``STREAM_GAP`` report (plan §11.2)."""
        async with self._sessions() as session:
            return await self._reconcile_gap_in_session(
                session,
                instance_id=instance_id,
                pane_id=pane_id,
                previous_stream_id=previous_stream_id,
                reason=reason,
                observed_at=observed_at,
                event_id=event_id,
            )

    async def evaluate_topology_change(
        self,
        instance_id: UUID,
        topology: TopologySnapshot,
        observed_at: datetime,
        event_id: UUID | None = None,
    ) -> list[FiredTrigger]:
        """Evaluate one topology snapshot for ``pane_exited`` watches."""
        previous = self._topologies.get(instance_id)
        self._topologies[instance_id] = topology
        if previous is None:
            return []
        trigger_event_id = event_id or uuid4()
        async with self._sessions() as session:
            triggers: list[FiredTrigger] = []
            fired_pairs: list[tuple[WatcherRuntime, FiredTrigger]] = []
            for runtime in self._runtimes_for(instance_id, None):
                if runtime.condition.kind is not WatchConditionKind.PANE_EXITED:
                    continue
                if not PaneExitDetector.exited(previous, topology, runtime.pane_id):
                    continue
                evidence = TriggerEvidence(
                    event_id=trigger_event_id,
                    source="topology_exit",
                    cursor=None,
                    observation_ref=build_observation_ref(
                        source="topology_exit",
                        instance_id=instance_id,
                        pane_id=runtime.pane_id,
                    ),
                )
                fired = await self._fire_in_session(session, runtime, evidence, observed_at)
                if fired is not None:
                    triggers.append(fired)
                    fired_pairs.append((runtime, fired))
            await session.commit()
        for runtime, fired in fired_pairs:
            self._apply_trigger_state(runtime, fired)
        await self._deliver(triggers)
        return triggers

    async def evaluate_recovered_cursor(
        self,
        cursor: PaneCursor,
        *,
        observed_at: datetime,
    ) -> None:
        """Accept a replayed/recovered cursor after restart or capture.

        A newer pane incarnation invalidates watches pinned to an older
        incarnation (plan §11.2: a Term restart or pane replacement
        invalidates old watches for that incarnation).
        """
        async with self._sessions() as session:
            acceptance, _ = await self._cursor_store.accept_recovered(
                session,
                instance_id=cursor.instance_id,
                pane_id=cursor.pane_id,
                stream_id=cursor.stream_id,
                seq=cursor.seq,
                pane_incarnation=cursor.pane_incarnation,
                observed_at=observed_at,
            )
            await session.commit()
        if acceptance is CursorAcceptance.INCARNATION_CHANGED:
            await self._invalidate_incarnation(
                cursor.instance_id, cursor.pane_id, cursor.pane_incarnation
            )
        else:
            for runtime in self._runtimes_for(cursor.instance_id, cursor.pane_id):
                runtime.last_cursor = cursor

    async def check_deadlines(self, now: datetime) -> list[FiredTrigger]:
        """Fire due ``output_idle`` deadlines (silence after an observed cursor)."""
        async with self._sessions() as session:
            triggers: list[FiredTrigger] = []
            fired_pairs: list[tuple[WatcherRuntime, FiredTrigger]] = []
            for runtime in list(self._watches.values()):
                if runtime.condition.kind is not WatchConditionKind.OUTPUT_IDLE:
                    continue
                if (runtime.instance_id, runtime.pane_id) in self._paused_panes:
                    # Unverified silence cannot fire an idle deadline.
                    continue
                if runtime.deadline is None or not runtime.deadline.due(now):
                    continue
                cursor = runtime.last_cursor
                if cursor is None:
                    continue
                evidence = TriggerEvidence(
                    event_id=uuid4(),
                    source="idle_deadline",
                    cursor=cursor,
                    observation_ref=build_observation_ref(
                        source="idle_deadline",
                        instance_id=runtime.instance_id,
                        pane_id=runtime.pane_id,
                        stream_id=cursor.stream_id,
                        seq=cursor.seq,
                    ),
                )
                fired = await self._fire_in_session(session, runtime, evidence, now)
                if fired is not None:
                    triggers.append(fired)
                    fired_pairs.append((runtime, fired))
            await session.commit()
        for runtime, fired in fired_pairs:
            self._apply_trigger_state(runtime, fired)
        await self._deliver(triggers)
        return triggers

    async def handle_wire_message(self, message: WireMessage) -> list[FiredTrigger]:
        """Dispatch one hub wire message (``PANE_OUTPUT``/``STREAM_GAP``/
        ``TOPOLOGY_CHANGED``); other messages are ignored.

        The trigger event id is derived from the stable ``message.message_id``
        so replaying the same wire message produces the same delivery key and
        can never wake the backend twice (plan §11.2).
        """
        if message.type is MessageType.PANE_OUTPUT:
            payload = PaneOutputPayload.model_validate(message.payload)
            return await self.evaluate_live_event(
                instance_id=message.instance_id,
                pane_id=payload.pane_id,
                stream_id=payload.stream_id,
                seq=payload.seq,
                data=payload.to_bytes(),
                # Idle timing uses B receipt time rather than A's clock.
                observed_at=self._clock(),
                event_id=message.message_id,
            )
        if message.type is MessageType.STREAM_GAP:
            payload = StreamGapPayload.model_validate(message.payload)
            return await self.evaluate_gap_event(
                instance_id=message.instance_id,
                pane_id=payload.pane_id,
                previous_stream_id=payload.previous_stream_id,
                reason=payload.reason,
                observed_at=self._clock(),
                event_id=message.message_id,
            )
        if message.type is MessageType.TOPOLOGY_CHANGED:
            payload = TopologyChangedPayload.model_validate(message.payload)
            return await self.evaluate_topology_change(
                instance_id=message.instance_id,
                topology=payload.topology,
                observed_at=self._clock(),
                event_id=message.message_id,
            )
        return []

    # ------------------------------------------------------------------
    # Trigger transaction
    # ------------------------------------------------------------------

    async def fire(
        self,
        runtime: WatcherRuntime,
        evidence: TriggerEvidence,
        observed_at: datetime,
    ) -> FiredTrigger | None:
        """Perform the atomic trigger transaction for one watcher.

        Cancellation, expiry, and evaluation race inside the same
        transaction: the watch row is re-fetched under the session, so a
        watch cancelled or expired between evaluation and the trigger can
        never produce an inbox item.
        """
        async with self._sessions() as session:
            fired = await self._fire_in_session(session, runtime, evidence, observed_at)
            await session.commit()
        if fired is not None:
            await self._deliver([fired])
        return fired

    async def _fire_in_session(
        self,
        session: AsyncSession,
        runtime: WatcherRuntime,
        evidence: TriggerEvidence,
        observed_at: datetime,
    ) -> FiredTrigger | None:
        """One atomic trigger inside the caller's session (no commit)."""
        row = await session.get(Watch, runtime.watch_id)
        if row is None or row.state != "active":
            return None
        expiry_at = _aware(row.expiry_at)
        if expiry_at is not None and expiry_at <= observed_at:
            row.state = "expired"
            return None
        delivery_key = self._delivery_key_for(runtime, evidence)
        existing = await session.scalar(
            select(WatchDelivery).where(WatchDelivery.delivery_key == delivery_key)
        )
        if existing is not None:
            # Duplicate delivery key: return the already-committed receipt and
            # its inbox item; never insert a second one or wake twice.
            inbox_item = None
            if existing.inbox_item_id is not None:
                inbox_item = await session.get(AgentInboxItem, existing.inbox_item_id)
            return FiredTrigger(
                watch=row,
                evidence=evidence,
                delivery=existing,
                inbox_item=inbox_item,
                deduped=True,
            )
        inbox_item = await _insert_inbox_item(
            session,
            conversation_id=row.conversation_id,
            kind="watch_triggered",
            actor_id=str(row.id),
            actor_kind="watch_engine",
            idempotency_key=delivery_key,
            payload_digest=hashlib.sha256(delivery_key.encode("utf-8")).hexdigest(),
            source="system",
            now=observed_at,
        )
        delivery = await _insert_watch_delivery(
            session,
            watch_id=row.id,
            delivery_key=delivery_key,
            trigger_event_id=evidence.event_id,
            inbox_item_id=inbox_item.id,
        )
        snapshot = self._snapshot_watch(row)
        if row.one_shot:
            row.state = "triggered"
        else:
            row.watch_generation = row.watch_generation + 1
            row.rearm_cursor = self._encode_cursor(evidence.cursor)
            if runtime.matcher is not None:
                # Rearm resets the matcher: a condition that stays true cannot
                # immediately retrigger until a new edge is observed.
                row.matcher_state = json.dumps(
                    LiteralMatcher(runtime.condition.match).state(),
                    separators=(",", ":"),
                )
        return FiredTrigger(
            watch=snapshot,
            evidence=evidence,
            delivery=delivery,
            inbox_item=inbox_item,
            deduped=False,
        )

    async def record_delivery_attempt(
        self,
        delivery_id: UUID,
        *,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> WatchDelivery | None:
        """Record one failed delivery attempt (plan §11.2)."""
        updated = await self._delivery_repo.record_attempt(
            delivery_id,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
        )
        if updated is not None:
            updated.next_attempt_at = _aware(updated.next_attempt_at)
        return updated

    def build_input(
        self,
        trigger: FiredTrigger,
        *,
        observed_at: datetime,
    ) -> WatchTriggeredInput | None:
        """Build the typed ``WatchTriggered`` AgentInput for a fired trigger."""
        if trigger is None or trigger.inbox_item is None:
            return None
        contract = decode_watch_start(trigger.watch.start_cursor)
        return build_watch_triggered_input(
            watch=trigger.watch,
            contract=contract,
            delivery_key=trigger.delivery.delivery_key,
            inbox_item=trigger.inbox_item,
            trigger_event_id=trigger.evidence.event_id,
            trigger_source=trigger.evidence.source,
            observed_at=observed_at,
            observation_ref=trigger.evidence.observation_ref,
            cursor=trigger.evidence.cursor,
            fired_generation=trigger.watch.watch_generation,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _deliver(self, triggers: list[FiredTrigger]) -> None:
        """Deliver fired triggers through the trigger sink (review fix M1).

        ``fire()`` committed the inbox row; the sink registers the typed
        payload with the binding's pipeline.  A sink failure never kills the
        evaluation: the trigger transaction is already durable and the
        dispatcher keeps the inbox item visible pending.
        """
        if self.on_fired is None:
            return
        for trigger in triggers:
            try:
                await self.on_fired(trigger)
            except Exception:
                logger.exception(
                    "Watch engine: trigger sink delivery failed for "
                    "delivery key %s",
                    trigger.delivery.delivery_key,
                )

    def _runtimes_for(
        self,
        instance_id: UUID,
        pane_id: str | None,
    ) -> list[WatcherRuntime]:
        return [
            runtime
            for runtime in self._watches.values()
            if runtime.instance_id == instance_id
            and (pane_id is None or runtime.pane_id == pane_id)
        ]

    @staticmethod
    def _consumes(runtime: WatcherRuntime, cursor: PaneCursor) -> bool:
        """Whether the engine may feed this event to the watcher's condition.

        Output at or before the creation cursor on the creation stream was
        consumed before the watch existed (old scrollback) and must never
        fire a new watch.
        """
        start = runtime.start_cursor
        if start is None:
            return True
        if cursor.stream_id == start.stream_id and cursor.seq <= start.seq:
            return False
        return True

    @staticmethod
    def _delivery_key_for(runtime: WatcherRuntime, evidence: TriggerEvidence) -> str:
        if evidence.source in ("live_output", "idle_deadline"):
            if evidence.cursor is None:
                raise ValueError(f"{evidence.source} evidence requires a cursor")
            return build_delivery_key(
                runtime.watch_id,
                runtime.generation,
                evidence.cursor.pane_incarnation,
                evidence.cursor.stream_id,
                evidence.cursor.seq,
            )
        if evidence.source == "gap_snapshot":
            if evidence.cursor is None:
                raise ValueError("gap_snapshot evidence requires a cursor")
            return build_delivery_key(
                runtime.watch_id,
                runtime.generation,
                evidence.cursor.pane_incarnation,
                "gap",
                evidence.event_id.hex,
            )
        if evidence.source == "topology_exit":
            return build_delivery_key(
                runtime.watch_id,
                runtime.generation,
                0,
                "exit",
                evidence.event_id.hex,
            )
        raise ValueError(f"unknown trigger source: {evidence.source!r}")

    @staticmethod
    def _encode_cursor(cursor: PaneCursor | None) -> str:
        if cursor is None:
            return json.dumps(None)
        return json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"))

    @staticmethod
    def _snapshot_watch(row: Watch) -> Watch:
        """Detached pre-transaction snapshot of the watch row.

        The trigger fired at this generation; the row itself may advance (or
        terminate) before the session closes.
        """
        return Watch(
            id=row.id,
            binding_id=row.binding_id,
            conversation_id=row.conversation_id,
            pane_id=row.pane_id,
            condition_kind=row.condition_kind,
            start_cursor=row.start_cursor,
            watch_generation=row.watch_generation,
            rearm_cursor=row.rearm_cursor,
            intent_summary=row.intent_summary,
            matcher_state=row.matcher_state,
            expiry_at=row.expiry_at,
            one_shot=row.one_shot,
            state=row.state,
            created_at=row.created_at,
        )

    def _apply_trigger_state(self, runtime: WatcherRuntime, fired: FiredTrigger) -> None:
        """Sync the in-memory runtime after a committed trigger."""
        if fired is None or fired.deduped:
            return
        if runtime.one_shot:
            self._watches.pop(runtime.watch_id, None)
            return
        runtime.generation += 1
        runtime.rearm_cursor = self._encode_cursor(fired.evidence.cursor)
        if runtime.matcher is not None:
            runtime.matcher = LiteralMatcher(runtime.condition.match)
        if runtime.deadline is not None:
            # Idle timing restarts only on the next verified cursor.
            runtime.deadline = IdleDeadline(runtime.condition.idle_after_seconds or 1)

    async def _reconcile_gap_in_session(
        self,
        session: AsyncSession,
        *,
        instance_id: UUID,
        pane_id: str,
        previous_stream_id: UUID,
        reason: str,
        observed_at: datetime,
        event_id: UUID | None,
    ) -> list[FiredTrigger]:
        """Bounded capture reconciliation for a gap (plan §11.2).

        Without a capture port (or when reconciliation fails) the pane is
        marked paused: continuity cannot be proven, so no watch may fire and
        ``output_idle`` cannot count unverified silence.  With a recovered
        anchor the observation is *indeterminate*: every active watch on the
        pane fires a ``gap_snapshot`` trigger that wakes the agent only to
        inspect; snapshot text is never fed to a live matcher.
        """
        if self._capture is None:
            self._paused_panes.add((instance_id, pane_id))
            return []
        reconciliation = await self._capture.reconcile(instance_id, pane_id)
        if reconciliation is None:
            self._paused_panes.add((instance_id, pane_id))
            return []
        trigger_event_id = event_id or uuid4()
        anchor = reconciliation.cursor
        acceptance, _ = await self._cursor_store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id=pane_id,
            stream_id=anchor.stream_id,
            seq=anchor.seq,
            pane_incarnation=anchor.pane_incarnation,
            observed_at=observed_at,
        )
        if acceptance is CursorAcceptance.INCARNATION_CHANGED:
            await self._invalidate_incarnation(instance_id, pane_id, anchor.pane_incarnation)
        self._paused_panes.discard((instance_id, pane_id))
        for runtime in self._runtimes_for(instance_id, pane_id):
            runtime.last_cursor = anchor
            if runtime.deadline is not None:
                # A verified recovered cursor restarts the idle timer.
                runtime.deadline.on_output(observed_at)
        observation_ref = build_observation_ref(
            source="gap_snapshot",
            instance_id=instance_id,
            pane_id=pane_id,
            stream_id=anchor.stream_id,
            seq=anchor.seq,
            byte_count=reconciliation.byte_count,
            excerpt=reconciliation.content,
        )
        triggers: list[FiredTrigger] = []
        fired_pairs: list[tuple[WatcherRuntime, FiredTrigger]] = []
        for runtime in self._runtimes_for(instance_id, pane_id):
            evidence = TriggerEvidence(
                event_id=trigger_event_id,
                source="gap_snapshot",
                cursor=anchor,
                observation_ref=observation_ref,
            )
            fired = await self._fire_in_session(session, runtime, evidence, observed_at)
            if fired is not None:
                triggers.append(fired)
                fired_pairs.append((runtime, fired))
        await session.commit()
        for runtime, fired in fired_pairs:
            self._apply_trigger_state(runtime, fired)
        await self._deliver(triggers)
        return triggers

    async def _invalidate_incarnation(
        self,
        instance_id: UUID,
        pane_id: str,
        incarnation: int,
    ) -> None:
        """Cancel active watches pinned to an older pane incarnation."""
        for runtime in list(self._runtimes_for(instance_id, pane_id)):
            start = runtime.start_cursor
            if start is not None and start.pane_incarnation < incarnation:
                await self._watch_repo.cancel(runtime.watch_id)
                self._watches.pop(runtime.watch_id, None)


# Re-export for callers that import the port types from this module.
__all__ = [
    "CursorAcceptance",
    "FiredTrigger",
    "GapReconciliation",
    "IdleDeadline",
    "LiteralMatcher",
    "ObservationCursorStore",
    "PaneExitDetector",
    "TriggerEvidence",
    "WatchContract",
    "WatchEngine",
    "WatcherRuntime",
    "build_delivery_key",
    "build_observation_ref",
    "build_watch_triggered_input",
    "decode_watch_start",
    "encode_watch_contract",
    "runtime_from_watch_row",
]
