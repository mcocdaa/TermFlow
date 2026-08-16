"""Per-binding orchestration: inbox → adapter → canonical timeline (M4.5 spec §1-§4).

``AgentPipelineService`` is the single coordination point for one Agent
Binding's runtime: it owns the dispatcher loop (claim inbox → render typed
turn → submit), the SSE consumer loop (normalized events → run state machine
→ canonical event cursor → message assembly), and the disconnect
reconcile/reconnect loop.

Serialization follows plan gate 6:

* one in-flight turn per conversation is enforced by
  :class:`~.inbox.InboxDeliveryStateMachine.claim_next` (the dispatched item
  stays in-flight until the M1.4 schema can persist terminal delivery);
* per-binding serialization is enforced inside this instance: the dispatcher
  handles exactly one envelope at a time and, after a confirmed submit,
  waits for the active run's terminal state (signalled by the SSE consumer)
  before claiming the next item;
* :meth:`~.runs.AgentRunStateMachine.one_active_run` is re-checked before
  submitting as a second line of defence against a running run left behind
  by recovery.

Fail-closed rules (M4.5 spec §2/§5/§7): a payload the in-process table cannot
render (for example after a B restart) exhausts a bounded grace and becomes
``delivery_unknown``; a submit outcome other than admission parks the inbox
item as ``delivery_unknown`` and fails the run — automatic retries are never
attempted.  Watch-triggered items are the exception (review fix M1): their
typed payload is registered asynchronously by the watch engine's trigger
sink after ``fire()`` commits the inbox row, so a payload-less watch item is
never claimed and never burned — it stays visible pending until the sink
registers (a B restart leaves it pending, which recovery keeps visible).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_protocol.agent import (
    AgentActorKind,
    AgentEventKind,
    AgentInputKind,
    AgentInputSource,
    BackendRuntimeState,
    UserMessageInput,
    UserMessagePayload,
    WatchTriggeredInput,
)

from termflow_control_plane.persistence.models import (
    AgentEvent,
    AgentInboxItem,
    AgentRun,
)
from termflow_control_plane.persistence.models import (
    BackendConversationRef as BackendConversationRefRow,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackend,
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
    InboxEnvelope,
)
from termflow_control_plane.plugins.agent_broker.agent.runs import (
    AgentRunStateMachine,
    RunBoundary,
    RunStateError,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SupervisorConnector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    AgentEventCursor,
    AgentStreamHub,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendCancelRequest,
    BackendConversationRef,
    BackendEventScope,
    BackendInteraction,
    BackendModel,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendTurnPart,
    BackendTurnRequest,
    BackendWorkspaceAlias,
    CreateBackendConversation,
    ProviderRef,
    TurnPartTrust,
)
from termflow_control_plane.plugins.protocol import RuntimeRef

logger = logging.getLogger(__name__)


def _adapter_attribute(adapter: AgentBackend, name: str) -> str:
    """Read a required string attribute an adapter is pinned to (fail loud).

    The structural ``AgentBackend`` protocol only declares behaviour; the
    per-binding pins ``runtime_id`` and ``directory`` are configuration the
    registry guarantees on every concrete adapter (spec §6).
    """
    value = getattr(adapter, name, None)
    if not isinstance(value, str) or not value:
        raise ValueError(f"backend adapter must expose a non-empty {name}")
    return value


def _publish_to_hub(hub: AgentStreamHub) -> Callable[[AgentEvent], Awaitable[None]]:
    """Adapter for the cursor publisher hook (discards slow-subscriber ids)."""

    async def publish(event: AgentEvent) -> None:
        await hub.publish(event)

    return publish


#: Grace window for the in-process payload table (spec §2 step 2): the watch
#: ``fire()`` commit and the ``submit_watch_input`` registration race in one
#: process, so the dispatcher retries a bounded number of times before
#: failing closed (a B restart can never deliver the payload).
_PAYLOAD_GRACE_ATTEMPTS = 10
_PAYLOAD_GRACE_DELAY_SECONDS = 0.5

#: Idle poll interval for the dispatcher when no item is claimable.
_DISPATCH_POLL_SECONDS = 0.05

#: Backoff used when ``one_active_run`` refuses a submission (fail closed,
#: fall back to waiting; the item re-enters the claim path after the backoff).
_ACTIVE_RUN_RETRY_SECONDS = 1.0

#: Reconnect backoff after an SSE disconnect (bounded, cap 30s per spec §5).
_RECONNECT_BACKOFF_BASE_SECONDS = 1.0
_RECONNECT_BACKOFF_CAP_SECONDS = 30.0

#: Run states that prove an event can no longer advance anything.
_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled", "unknown"})

#: Run states that can still receive backend events.
_ACTIVE_RUN_STATES = ("queued", "running")

#: Bounded keyset scan for the dispatcher claim walk (review fix major-1): a
#: page of claimable candidates may be fully blocked by payload-less watch
#: rows, so the scan advances past them up to this many pages (10 rows each)
#: per dispatch poll instead of starving later items.
_CLAIM_SCAN_MAX_PAGES = 20

#: Payload-less watch rows older than this are alerted: the typed payload may
#: never arrive (B restart, sink failure), so silence would hide the stall.
_STALE_WATCH_WARN_SECONDS = 60.0

#: Rate limit for the stale-watch warning log: the alert counter increments
#: on every observation, but the log fires at most once per interval.
_STALE_WATCH_LOG_INTERVAL_SECONDS = 30.0


def _canonical_payload_json(notification: BackendNotification) -> str:
    """Serialize one normalized notification into the canonical payload JSON.

    A single, deterministic JSON string feeds both ``payload_json`` and
    ``payload_digest`` so the repository's digest invariant
    (``sha256(payload_json) == payload_digest``, enforced by
    :class:`AgentEventRepository`) can never drift.
    """
    payload: dict[str, object] = {
        "kind": notification.kind.value,
        "run_id": str(notification.run_id) if notification.run_id is not None else None,
        "message_id": (
            str(notification.message_id) if notification.message_id is not None else None
        ),
        "part_id": notification.part_id,
        "tool_call_id": notification.tool_call_id,
        "text": notification.payload.text,
        "summary": notification.payload.summary,
        "error_code": notification.payload.error_code,
        "error_message": notification.payload.error_message,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class PipelineDiagnostics:
    """Bounded, identifier-only counters for dropped/failed pipeline work.

    Content is never logged or stored: only identifiers and reasons (spec §6
    diagnostic redaction).
    """

    ownership_dropped: int = 0
    unknown_conversation_dropped: int = 0
    no_active_run_dropped: int = 0
    missing_payload_delivery_unknown: int = 0
    watch_payload_pending_skipped: int = 0
    #: Payload-less watch rows observed past the stale-age threshold.
    watch_payload_stale_alerted: int = 0
    #: Bounded claim scans that hit the page limit without claiming anything.
    claim_scan_exhausted: int = 0
    run_start_idempotent: int = 0
    submits_failed: int = 0
    reconnects: int = 0


class SubmitAdmission(BackendModel):
    """Durable admission receipt for one accepted user message (spec §2).

    The product API surfaces this as ``202 Accepted``; the backend's 204
    confirmation is adapter-internal and never crosses this boundary.
    """

    message_id: UUID
    conversation_id: UUID
    admission_seq: int
    idempotency_key: str
    delivery_state: str
    submission_state: str


class CancelResult(BackendModel):
    """Product-neutral result of cancelling one active Agent run."""

    outcome: BackendOutcome
    run_id: UUID
    run_state: str


class NoActiveRunError(Exception):
    """Raised when cancellation is requested without a queued/running run."""


def _submit_error_code(outcome: BackendOutcome) -> str:
    """Map a failed submit outcome onto the plan §7 error vocabulary."""
    if outcome is BackendOutcome.UNSUPPORTED:
        return "submit_rejected"
    if outcome is BackendOutcome.RETRYABLE:
        return "submit_retryable"
    return "submit_unknown"


class AgentPipelineService:
    """Per-binding orchestration: inbox → adapter → canonical timeline.

    One instance per binding (M4.5 spec §1): the dispatcher and the SSE
    consumer share the active-run signal, the in-process payload table, and
    the adapter/scope, which is exactly the state a scatter of hooks cannot
    coordinate safely.
    """

    def __init__(
        self,
        *,
        binding_id: UUID,
        adapter: AgentBackend,
        scope: BackendEventScope,
        capabilities: AgentBackendCapabilities,
        repositories: RepositoryBundle,
        sessions: async_sessionmaker[AsyncSession],
        hub: AgentStreamHub,
        supervisor: SupervisorConnector | None,
        runtime_ref: str,
        runtime_epoch: int,
        now: Callable[[], datetime] | None = None,
        payload_grace_attempts: int = _PAYLOAD_GRACE_ATTEMPTS,
        payload_grace_delay: float = _PAYLOAD_GRACE_DELAY_SECONDS,
        dispatch_poll_seconds: float = _DISPATCH_POLL_SECONDS,
        active_run_retry_seconds: float = _ACTIVE_RUN_RETRY_SECONDS,
        reconnect_backoff_base: float = _RECONNECT_BACKOFF_BASE_SECONDS,
        reconnect_backoff_cap: float = _RECONNECT_BACKOFF_CAP_SECONDS,
        reconcile_attempts: int = 5,
        cancel_wait_timeout: float = 5.0,
        claim_scan_max_pages: int = _CLAIM_SCAN_MAX_PAGES,
        stale_watch_warn_seconds: float = _STALE_WATCH_WARN_SECONDS,
    ) -> None:
        self.binding_id = binding_id
        self._adapter = adapter
        self._scope = scope
        self._capabilities = capabilities
        self._repositories = repositories
        self._sessions = sessions
        self._supervisor = supervisor
        self._runtime_ref = RuntimeRef(runtime_ref)
        self._runtime_epoch = runtime_epoch
        self._now = now or (lambda: datetime.now(UTC))
        # The adapter is pinned to one runtime and one workspace directory
        # (spec §6); both must be exposed for ownership checks and session
        # creation, so they are captured (and validated) at construction.
        self._adapter_runtime_id = _adapter_attribute(adapter, "runtime_id")
        self._adapter_directory = _adapter_attribute(adapter, "directory")
        # Test seams: the M4.5 spec constants with injectable overrides.
        self._payload_grace_attempts = payload_grace_attempts
        self._payload_grace_delay = payload_grace_delay
        self._dispatch_poll_seconds = dispatch_poll_seconds
        self._active_run_retry_seconds = active_run_retry_seconds
        self._reconnect_backoff_base = reconnect_backoff_base
        self._reconnect_backoff_cap = reconnect_backoff_cap
        if reconcile_attempts < 1:
            raise ValueError("reconcile_attempts must be at least 1")
        if cancel_wait_timeout < 0:
            raise ValueError("cancel_wait_timeout must be non-negative")
        if claim_scan_max_pages < 1:
            raise ValueError("claim_scan_max_pages must be at least 1")
        if stale_watch_warn_seconds < 0:
            raise ValueError("stale_watch_warn_seconds must be non-negative")
        self._reconcile_attempts = reconcile_attempts
        self._cancel_wait_timeout = cancel_wait_timeout
        self._claim_scan_max_pages = claim_scan_max_pages
        self._stale_watch_warn_seconds = stale_watch_warn_seconds
        # Throttle bookkeeping for the major-1 warning logs (counters live in
        # self.diagnostics and increment on every observation).
        self._last_stale_watch_log_at: datetime = datetime.min.replace(tzinfo=UTC)
        self._scan_exhaustion_logged = False

        self.inbox_machine = InboxDeliveryStateMachine(
            repositories.agent_inbox,
            now=self._now,
            capabilities=capabilities,
            worker_id=f"pipeline:{binding_id}",
            # Review fix M1: a watch item whose typed payload has not been
            # registered by the trigger sink yet is never claimed - it stays
            # visible pending instead of burning the continuation.
            claim_filter=self._claim_eligible,
        )
        self.run_machine = AgentRunStateMachine(repositories.agent_runs, now=self._now)
        self._cursor = AgentEventCursor(
            repositories.agent_events,
            sessions,
            publisher=_publish_to_hub(hub),
        )
        self.diagnostics = PipelineDiagnostics()

        # In-process typed payloads (spec §2): inbox rows store only
        # payload_digest, so the dispatcher renders from this table.  A B
        # restart loses it; unclaimable items fail closed (risk 1).
        self._pending_payloads: dict[UUID, UserMessageInput | WatchTriggeredInput] = {}
        # Wake the dispatcher when work is registered.
        self._wake = asyncio.Event()

        # Per-binding serialization: after a confirmed submit the dispatcher
        # waits for the active run's terminal signal (set by the SSE consumer).
        self._active_run_id: UUID | None = None
        self._run_terminal_event: asyncio.Event | None = None
        # Same-process link from a run to the dispatched inbox envelope.  The
        # current schema has no run_id on inbox rows; this lets runtime
        # reconcile park the exact delivery without pretending restart-safe
        # recovery (restart fencing remains run_agent_recovery's job).
        self._run_envelopes: dict[UUID, InboxEnvelope] = {}

        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._adapter_closed = False
        self._reconnect_attempts = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def adapter(self) -> AgentBackend:
        """The runtime adapter this pipeline orchestrates (registry-owned)."""
        return self._adapter

    @property
    def started(self) -> bool:
        """Whether ``start()`` has spawned the dispatcher and SSE consumer."""
        return self._started

    async def start(self) -> None:
        """Spawn the dispatcher and the SSE consumer tasks (idempotent)."""
        if self._started:
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name=f"agent-dispatch-{self.binding_id}"),
            asyncio.create_task(self._consume_events(), name=f"agent-events-{self.binding_id}"),
        ]

    async def stop(self) -> None:
        """Cancel both tasks, then release the adapter (idempotent)."""
        self._started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if not self._adapter_closed:
            self._adapter_closed = True
            await self._adapter.close()

    # ------------------------------------------------------------------
    # Product entries
    # ------------------------------------------------------------------

    async def submit_user_message(
        self, conversation_id: UUID, text: str, *, actor: str
    ) -> SubmitAdmission:
        """Admit one user message: durable enqueue + in-process payload (spec §2).

        The enqueue is the durable admission (204 semantics stay
        adapter-internal); the typed payload is registered for the dispatcher
        in-process so the turn can be rendered without a payload column.

        """
        idempotency_key = str(uuid4())
        correlation_id = uuid4()
        payload_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item = await self._repositories.agent_inbox.enqueue(
            conversation_id=conversation_id,
            kind=AgentInputKind.USER_MESSAGE.value,
            actor_id=actor,
            actor_kind=AgentActorKind.USER_SESSION.value,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            source=AgentInputSource.USER.value,
            correlation_id=correlation_id,
        )
        self._pending_payloads[item.id] = UserMessageInput(
            kind="user_message",
            input_id=item.id,
            conversation_id=conversation_id,
            actor_id=actor,
            actor_kind=AgentActorKind.USER_SESSION,
            admission_seq=item.admission_seq,
            idempotency_key=idempotency_key,
            source=AgentInputSource.USER,
            causation_id=str(uuid4()),
            correlation_id=str(correlation_id),
            payload=UserMessagePayload(text=text),
        )
        self._wake.set()
        return SubmitAdmission(
            message_id=item.id,
            conversation_id=conversation_id,
            admission_seq=item.admission_seq,
            idempotency_key=idempotency_key,
            delivery_state="pending",
            submission_state="not_started",
        )

    async def submit_watch_input(self, input: WatchTriggeredInput) -> None:
        """Register a fired watch's typed input for the dispatcher (spec §3).

        The inbox row was already inserted atomically by ``WatchEngine.fire``;
        this only bridges the typed payload (keyed by the inbox item id) to
        the in-process table and wakes the dispatcher.
        """
        item = await self._repositories.agent_inbox.get_by_idempotency_key(input.idempotency_key)
        if item is None:
            logger.warning(
                "Agent pipeline %s: watch input (idempotency_key=%s, conversation=%s) "
                "has no inbox item; the dispatcher will fail it closed if it can "
                "never be rendered",
                self.binding_id,
                input.idempotency_key,
                input.conversation_id,
            )
            return
        self._pending_payloads[item.id] = input
        self._wake.set()

    async def cancel_conversation(
        self, conversation_id: UUID, *, reason: str | None
    ) -> CancelResult:
        """Cancel the current run, proving a terminal state or marking unknown."""
        run = await self._active_run_for_conversation(conversation_id)
        if run is None:
            raise NoActiveRunError(f"conversation {conversation_id} has no active run")
        ref = await self._existing_backend_ref(conversation_id)
        result = await self._adapter.cancel(
            BackendCancelRequest(
                conversation_ref=ref,
                run_id=run.id,
                correlation_id=str(uuid4()),
                reason=reason,
            )
        )
        if result.outcome is BackendOutcome.CONFIRMED:
            terminal = await self._wait_for_terminal_state(
                run.id, wait_seconds=self._cancel_wait_timeout
            )
            if terminal is not None:
                return CancelResult(
                    outcome=BackendOutcome.CONFIRMED,
                    run_id=run.id,
                    run_state=terminal,
                )
            snapshot = await self._adapter.reconcile(ref)
            if snapshot.outcome is BackendOutcome.CONTEXT_LOST or (
                snapshot.outcome is BackendOutcome.CONFIRMED
                and snapshot.state is BackendRuntimeState.CLOSED
            ):
                await self._transition_run_terminal(run.id, "cancelled")
                self._signal_run_terminal(run.id)
                self._run_envelopes.pop(run.id, None)
                return CancelResult(
                    outcome=BackendOutcome.CONFIRMED,
                    run_id=run.id,
                    run_state="cancelled",
                )
            # The backend confirmed the cancellation request, but the session
            # is still busy (or the proof itself is uncertain): the run cannot
            # be proven cancelled, so it is marked unknown (spec §8).
            await self._transition_run_terminal(run.id, "unknown")
            self._signal_run_terminal(run.id)
            self._run_envelopes.pop(run.id, None)
            return CancelResult(
                outcome=BackendOutcome.CONFIRMED,
                run_id=run.id,
                run_state="unknown",
            )

        # A non-confirmed cancel outcome (transport failure, rejection, or any
        # other adapter result) proves nothing: the run is marked unknown and
        # the product outcome is reported as "unknown" (spec §8 never lets a
        # non-confirmed adapter outcome masquerade as a cancellation).
        await self._transition_run_terminal(run.id, "unknown")
        self._signal_run_terminal(run.id)
        self._run_envelopes.pop(run.id, None)
        return CancelResult(
            outcome=BackendOutcome.UNKNOWN,
            run_id=run.id,
            run_state="unknown",
        )

    async def resolve_permission(self, interaction: BackendInteraction) -> BackendOperationResult:
        """Forward a normalized one-shot permission decision to the adapter."""
        return await self._adapter.interact(interaction)

    # ------------------------------------------------------------------
    # Dispatcher loop (spec §2)
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                envelope = await self._claim_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Agent pipeline %s: inbox claim failed; retrying shortly",
                    self.binding_id,
                )
                await asyncio.sleep(self._dispatch_poll_seconds)
                continue
            if envelope is None:
                await self._wait_for_work()
                continue
            try:
                await self._dispatch_envelope(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fail loud but never die: the claim lease bounds the item's
                # wait, and the loop keeps serving other conversations.
                logger.exception(
                    "Agent pipeline %s: dispatch of inbox item %s failed",
                    self.binding_id,
                    envelope.id,
                )
                await asyncio.sleep(self._dispatch_poll_seconds)

    async def _claim_next(self) -> InboxEnvelope | None:
        """Claim the next inbox item for *this binding's* conversations.

        The inbox is shared across bindings, so claim candidates are scoped
        through the binding join (M0 isolation: a pipeline never routes
        another binding's input to its own runtime).  The binding-scoped
        query replaces the previous ``list_for_binding`` (default limit 50)
        iteration, which silently starved the 51st+ conversation's items.

        The candidate pages are walked with a bounded keyset scan (review
        fix major-1): a page fully blocked by payload-less watch rows no
        longer starves the items behind it, because the scan advances past
        it up to ``claim_scan_max_pages`` pages.  Aged payload-less watch
        rows are alerted instead of skipped silently.  The inbox machine
        still owns the CAS claim, the one-in-flight gate, and the parked
        filter.
        """
        stale: list[AgentInboxItem] = []
        tried: set[UUID] = set()
        cursor: tuple[int, UUID] | None = None
        for _ in range(self._claim_scan_max_pages):
            rows = await self._repositories.agent_inbox.next_pending_for_binding(
                self.binding_id, after=cursor
            )
            if not rows:
                # All pending rows for this binding were examined.
                break
            cursor = (rows[-1].admission_seq, rows[-1].conversation_id)
            for row in rows:
                if not self._claim_eligible(row):
                    # Waiting for the trigger sink to register the typed
                    # payload: the item stays visible pending (review fix M1)
                    # instead of being claimed and burned as delivery_unknown.
                    self.diagnostics.watch_payload_pending_skipped += 1
                    if self._watch_row_age_seconds(row) >= self._stale_watch_warn_seconds:
                        stale.append(row)
                    continue
                if row.conversation_id in tried:
                    continue
                tried.add(row.conversation_id)
                envelope = await self.inbox_machine.claim_next(
                    conversation_id=row.conversation_id
                )
                if envelope is not None:
                    self._alert_stale_watch_rows(stale)
                    return envelope
        else:
            # The bounded scan hit its page limit without claiming anything:
            # record the exhaustion so a backlog beyond the window is visible
            # in diagnostics instead of silently spinning.
            self.diagnostics.claim_scan_exhausted += 1
            if not self._scan_exhaustion_logged:
                self._scan_exhaustion_logged = True
                logger.warning(
                    "Agent pipeline %s: claim scan exhausted its bound (%d pages) "
                    "without claiming an item; the inbox backlog exceeds the "
                    "scan window",
                    self.binding_id,
                    self._claim_scan_max_pages,
                )
        self._alert_stale_watch_rows(stale)
        return None

    def _claim_eligible(self, row: AgentInboxItem) -> bool:
        """Pre-claim gate (review fix M1).

        Watch items whose typed payload has not been registered yet by the
        trigger sink are skipped: the claim is not consumed and the item
        stays visible pending (a B restart can never deliver the payload,
        so a permanently missing payload keeps the item pending rather than
        burning the continuation as ``delivery_unknown``).
        """
        if row.kind != "watch_triggered":
            return True
        return row.id in self._pending_payloads

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        """SQLite round-trips datetimes naive; attach UTC for age math."""
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _watch_row_age_seconds(self, row: AgentInboxItem) -> float:
        return (self._now() - self._aware_utc(row.created_at)).total_seconds()

    def _alert_stale_watch_rows(self, stale: list[AgentInboxItem]) -> None:
        """Count and (rate-limited) warn about aged payload-less watch rows.

        Review fix major-1: a payload-less watch row stuck pending is a
        permanent stall unless the sink registers a payload, so age-past-
        threshold rows must never stay silent.  Identifiers only - the
        payload is definitionally absent and never logged (spec §6).
        """
        if not stale:
            return
        self.diagnostics.watch_payload_stale_alerted += len(stale)
        now = self._now()
        if (
            now - self._last_stale_watch_log_at
        ).total_seconds() < _STALE_WATCH_LOG_INTERVAL_SECONDS:
            return
        self._last_stale_watch_log_at = now
        oldest = stale[0]
        logger.warning(
            "Agent pipeline %s: %d payload-less watch inbox row(s) stuck "
            "pending for longer than %.0fs (no trigger sink payload); oldest "
            "item %s (conversation %s, age %.0fs)",
            self.binding_id,
            len(stale),
            self._stale_watch_warn_seconds,
            oldest.id,
            oldest.conversation_id,
            self._watch_row_age_seconds(oldest),
        )

    async def _wait_for_work(self) -> None:
        """Idle-wait for new inbox work, bounded by a poll tick."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._dispatch_poll_seconds)
        except TimeoutError:
            pass

    async def _dispatch_envelope(self, envelope: InboxEnvelope) -> None:
        # 1) Payload lookup in the in-process table with a bounded grace
        #    window (spec §2 step 2); exhaustion fails closed.
        payload = await self._lookup_payload(envelope)
        if payload is None:
            if envelope.kind == "watch_triggered":
                # Review fix M1 (defense in depth): the claim filter normally
                # keeps payload-less watch items out of the dispatch path.
                # If one still arrives here, never burn the continuation:
                # leave the row alone so the claim lease expires and the
                # item re-enters the claim path once the sink registers.
                self.diagnostics.watch_payload_pending_skipped += 1
                logger.warning(
                    "Agent pipeline %s: watch inbox item %s (conversation %s) "
                    "has no in-process payload; leaving it pending for the "
                    "trigger sink",
                    self.binding_id,
                    envelope.id,
                    envelope.conversation_id,
                )
                return
            self.diagnostics.missing_payload_delivery_unknown += 1
            logger.warning(
                "Agent pipeline %s: inbox item %s (conversation %s) has no "
                "in-process payload after %d grace attempts; failing closed",
                self.binding_id,
                envelope.id,
                envelope.conversation_id,
                self._payload_grace_attempts,
            )
            await self.inbox_machine.mark_delivery_unknown(envelope)
            return

        # 2) one_active_run double insurance (spec §7): a running run left
        #    behind by recovery refuses the submission; the item falls back
        #    to waiting instead of racing the unknown run.
        if await self.run_machine.one_active_run(envelope.conversation_id):
            logger.warning(
                "Agent pipeline %s: conversation %s still has an active run; "
                "refusing inbox item %s and falling back to waiting",
                self.binding_id,
                envelope.conversation_id,
                envelope.id,
            )
            await self.inbox_machine.mark_retry(
                envelope, backoff=timedelta(seconds=self._active_run_retry_seconds)
            )
            if envelope.delivery_state == "dead_letter":
                # The bounded retry walk ended terminally (review m2): the
                # in-process payload has served its only purpose and must
                # not leak.
                self._pending_payloads.pop(envelope.id, None)
            return

        # 3) Backend conversation ref: reuse or create + persist (spec §2 step 3).
        ref = await self._resolve_backend_ref(envelope.conversation_id)

        # 4) Create the run in queued.
        run_id = await self.run_machine.create_for_conversation(envelope.conversation_id)

        # 5) Commit started before any network write (spec §2 step 5).
        started = await self.inbox_machine.mark_started(
            envelope,
            attempt_id=str(uuid4()),
            stable_message_id=envelope.idempotency_key,
            # A claimed envelope always carries its fencing owner; the state
            # machine validates it against the live claim.
            owner=cast(str, envelope.claim_owner),
            fencing_token=str(uuid4()),
        )
        self._run_envelopes[run_id] = started

        # 6) Supervisor re-check before submitting (spec §2 step 6): epoch
        #    drift fails closed instead of shipping a stale turn.  A raising
        #    gate (runtime manager unreachable) fails closed exactly like a
        #    rejection (review m2) - either way no in-process state leaks.
        if self._supervisor is not None:
            try:
                accepted = self._supervisor.accept_activation(
                    self._runtime_ref, self._runtime_epoch
                )
            except Exception:
                logger.exception(
                    "Agent pipeline %s: supervisor activation check raised; "
                    "failing closed",
                    self.binding_id,
                )
                self._pending_payloads.pop(envelope.id, None)
                await self._fail_run_and_delivery(
                    run_id, started, error_code="runtime_not_ready"
                )
                return
            if not accepted:
                self._pending_payloads.pop(envelope.id, None)
                await self._fail_run_and_delivery(
                    run_id, started, error_code="runtime_not_ready"
                )
                return

        # 7) Render + submit (spec §2 step 7).
        try:
            request = self._render_turn_request(started, payload, ref)
        except ValueError as exc:
            logger.warning(
                "Agent pipeline %s: inbox item %s cannot be rendered (%s); failing closed",
                self.binding_id,
                envelope.id,
                exc,
            )
            self._pending_payloads.pop(envelope.id, None)
            await self._fail_run_and_delivery(run_id, started, error_code="unrenderable")
            return
        # The turn is rendered: the in-process payload has served its only
        # purpose and is dropped (the dispatched item is never re-claimed).
        self._pending_payloads.pop(envelope.id, None)

        try:
            result = await self._adapter.submit(ref, request)
        except Exception:
            logger.exception(
                "Agent pipeline %s: submit of inbox item %s raised; failing closed",
                self.binding_id,
                envelope.id,
            )
            await self._fail_run_and_delivery(run_id, started, error_code="submit_retryable")
            return

        if result.outcome in (BackendOutcome.CONFIRMED, BackendOutcome.REQUESTED):
            # Admission accepted: the run stays queued until RUN_STARTED.
            # Per-binding serialization: wait for the run's terminal signal
            # before claiming the next envelope (spec §7).
            await self._wait_for_run_terminal(run_id)
            return

        await self._fail_run_and_delivery(
            run_id, started, error_code=_submit_error_code(result.outcome)
        )

    async def _fail_run_and_delivery(
        self, run_id: UUID, envelope: InboxEnvelope, *, error_code: str
    ) -> None:
        """Fail a queued run and park its delivery; never auto-retry.

        The run state machine only allows terminal transitions from
        ``running``, so the run is started first and then failed; the inbox
        item becomes ``delivery_unknown`` (plan §7: uncertain delivery is a
        visible recoverable state, never an automatic duplicate action).
        The run-to-envelope link is released so the failure path cannot
        leak it (review m1).
        """
        self._run_envelopes.pop(run_id, None)
        self.diagnostics.submits_failed += 1
        try:
            await self.run_machine.start(run_id)
        except RunStateError:
            # RUN_STARTED raced the failure path: the run is already running.
            pass
        try:
            await self.run_machine.fail(run_id, error_code=error_code)
        except RunStateError:
            # Already terminal: the failure intent is satisfied.
            logger.warning(
                "Agent pipeline %s: run %s was already terminal when the "
                "submission failure %r was recorded",
                self.binding_id,
                run_id,
                error_code,
            )
        await self.inbox_machine.mark_delivery_unknown(envelope)

    async def _lookup_payload(
        self, envelope: InboxEnvelope
    ) -> UserMessageInput | WatchTriggeredInput | None:
        for attempt in range(self._payload_grace_attempts):
            payload = self._pending_payloads.get(envelope.id)
            if payload is not None:
                return payload
            if attempt + 1 < self._payload_grace_attempts:
                await asyncio.sleep(self._payload_grace_delay)
        return None

    async def _resolve_backend_ref(self, conversation_id: UUID) -> BackendConversationRef:
        """Reuse the persisted backend conversation ref, or create + persist one.

        Returns the neutral :class:`~.turns.BackendConversationRef` model
        (the repository rows are ORM objects and never cross the adapter
        boundary).
        """
        existing = await self._repositories.agent_backend_conversations.get_by_conversation(
            conversation_id
        )
        if existing is not None:
            return self._to_turns_ref(existing)
        conversation = await self._repositories.agent_conversations.get_by_id(conversation_id)
        title = (
            conversation.title
            if conversation is not None and conversation.title
            else f"conversation-{conversation_id}"
        )
        created = await self._adapter.create_conversation(
            CreateBackendConversation(
                display_title=title,
                workspace_alias=BackendWorkspaceAlias(self._adapter_directory),
            )
        )
        persisted = await self._repositories.agent_backend_conversations.create(
            conversation_id=conversation_id,
            backend_kind=created.backend_kind,
            runtime_id=created.runtime_id,
            binding_capability_epoch=created.binding_capability_epoch,
            provider_ref=created.provider_ref,
            backend_version=created.backend_version,
        )
        return self._to_turns_ref(persisted)

    async def _existing_backend_ref(self, conversation_id: UUID) -> BackendConversationRef:
        row = await self._repositories.agent_backend_conversations.get_by_conversation(
            conversation_id
        )
        if row is None:
            raise NoActiveRunError(f"conversation {conversation_id} has no backend conversation")
        return self._to_turns_ref(row)

    async def _active_run_for_conversation(self, conversation_id: UUID) -> AgentRun | None:
        runs = await self._repositories.agent_runs.list_for_conversation(conversation_id)
        return next((run for run in runs if run.run_state in _ACTIVE_RUN_STATES), None)

    async def _wait_for_terminal_state(self, run_id: UUID, *, wait_seconds: float) -> str | None:
        current = await self._repositories.agent_runs.get_by_id(run_id)
        if current is not None and current.run_state in _TERMINAL_RUN_STATES:
            return current.run_state
        event = self._run_terminal_event if self._active_run_id == run_id else None
        if event is not None and wait_seconds > 0:
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
        current = await self._repositories.agent_runs.get_by_id(run_id)
        if current is not None and current.run_state in _TERMINAL_RUN_STATES:
            return current.run_state
        return None

    async def _transition_run_terminal(
        self,
        run_id: UUID,
        state: str,
        *,
        error_code: str | None = None,
    ) -> None:
        """Move queued/running work to a terminal state without weakening CAS."""
        current = await self._repositories.agent_runs.get_by_id(run_id)
        if current is None or current.run_state in _TERMINAL_RUN_STATES:
            return
        if current.run_state == "queued":
            try:
                await self.run_machine.start(run_id)
            except RunStateError:
                # A concurrent RUN_STARTED may win queued -> running.  Re-read
                # below and terminate that authoritative state instead of
                # treating the expected CAS race as cancellation failure.
                pass
            current = await self._repositories.agent_runs.get_by_id(run_id)
            if current is None or current.run_state in _TERMINAL_RUN_STATES:
                return
            if current.run_state != "running":
                return
        try:
            if state == "failed":
                await self.run_machine.fail(run_id, error_code=error_code or "backend_failed")
            elif state == "cancelled":
                await self.run_machine.cancel(run_id)
            elif state == "unknown":
                await self.run_machine.mark_unknown(run_id)
            else:  # pragma: no cover - all callers use the closed vocabulary
                raise ValueError(f"unsupported terminal run state: {state}")
        except RunStateError:
            # A boundary event can win the transition race.  Its durable state
            # is authoritative, so reconciliation never overwrites it.
            return

    async def _park_run_delivery(self, run_id: UUID) -> None:
        envelope = self._run_envelopes.pop(run_id, None)
        if envelope is not None:
            await self.inbox_machine.mark_delivery_unknown(envelope)

    @staticmethod
    def _to_turns_ref(row: BackendConversationRefRow) -> BackendConversationRef:
        """Project a persisted ORM ref row onto the neutral turns model."""
        return BackendConversationRef(
            backend_kind=row.backend_kind,
            backend_version=cast(str, row.backend_version),
            runtime_id=row.runtime_id,
            binding_capability_epoch=row.binding_capability_epoch,
            provider_ref=ProviderRef(row.provider_ref),
        )

    def _render_turn_request(
        self,
        envelope: InboxEnvelope,
        payload: UserMessageInput | WatchTriggeredInput,
        ref: BackendConversationRef,
    ) -> BackendTurnRequest:
        if isinstance(payload, UserMessageInput):
            parts = (
                BackendTurnPart(
                    kind=AgentInputKind.USER_MESSAGE,
                    text=payload.payload.text,
                    trust=TurnPartTrust.TRUSTED,
                ),
            )
        elif isinstance(payload, WatchTriggeredInput):
            text = payload.payload.continuation
            if text is None:
                raise ValueError("watch triggered input requires continuation text")
            parts = (
                BackendTurnPart(
                    kind=AgentInputKind.WATCH_TRIGGERED,
                    text=text,
                    trust=TurnPartTrust.UNTRUSTED,
                ),
            )
        else:  # pragma: no cover - the payload table only holds these two kinds
            raise ValueError(f"unsupported input kind: {type(payload).__name__}")
        return BackendTurnRequest(
            conversation_ref=ref,
            correlation_id=str(envelope.correlation_id),
            idempotency_key=envelope.idempotency_key,
            parts=parts,
        )

    async def _wait_for_run_terminal(self, run_id: UUID) -> None:
        """Wait for the active run's terminal signal (per-binding serialization)."""
        event = asyncio.Event()
        self._active_run_id = run_id
        self._run_terminal_event = event
        # The SSE consumer may have completed the run before this point; the
        # durable state check closes that race.
        run = await self._repositories.agent_runs.get_by_id(run_id)
        if run is not None and run.run_state in _ACTIVE_RUN_STATES:
            await event.wait()
        self._active_run_id = None
        self._run_terminal_event = None

    def _signal_run_terminal(self, run_id: UUID) -> None:
        if run_id == self._active_run_id and self._run_terminal_event is not None:
            self._run_terminal_event.set()

    # ------------------------------------------------------------------
    # SSE consumer loop (spec §4)
    # ------------------------------------------------------------------

    async def _consume_events(self) -> None:
        while True:
            try:
                async for notification in self._adapter.events(self._scope):
                    await self._handle_notification(notification)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Agent pipeline %s: SSE consumption raised; reconnecting",
                    self.binding_id,
                )
            # The iterator ended cleanly (non-shutdown): reconcile + reconnect.
            # An operational fault inside reconcile must not kill the
            # per-binding consumer either (spec §5 keeps retrying until
            # stop()): pace the retry with the base backoff and loop.
            try:
                await self._reconcile_and_reconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Agent pipeline %s: reconcile/reconnect raised; retrying",
                    self.binding_id,
                )
                await asyncio.sleep(min(self._reconnect_backoff_base, self._reconnect_backoff_cap))

    async def _handle_notification(self, notification: BackendNotification) -> None:
        self._reconnect_attempts = 0  # stream is healthy again

        # 1) Ownership: binding scope + runtime id must match (M0 contract).
        if str(self.binding_id) != notification.scope.binding_id:
            self.diagnostics.ownership_dropped += 1
            logger.warning(
                "Agent pipeline %s: dropping event %s from foreign binding %s",
                self.binding_id,
                notification.dedup_key,
                notification.scope.binding_id,
            )
            return
        if notification.conversation_ref.runtime_id != self._adapter_runtime_id:
            self.diagnostics.ownership_dropped += 1
            logger.warning(
                "Agent pipeline %s: dropping event %s from foreign runtime %s",
                self.binding_id,
                notification.dedup_key,
                notification.conversation_ref.runtime_id,
            )
            return

        # 2) Conversation resolution through the persisted opaque ref.
        ref_row = await self._repositories.agent_backend_conversations.get_by_provider_ref(
            notification.conversation_ref.provider_ref
        )
        if ref_row is None:
            self.diagnostics.unknown_conversation_dropped += 1
            logger.warning(
                "Agent pipeline %s: dropping event %s for unknown provider ref %s",
                self.binding_id,
                notification.dedup_key,
                notification.conversation_ref.provider_ref,
            )
            return
        conversation_id = ref_row.conversation_id

        # 3) Run mapping: the conversation's current queued/running run
        #    (newest first).
        runs = await self._repositories.agent_runs.list_for_conversation(conversation_id)
        active = next((run for run in runs if run.run_state in _ACTIVE_RUN_STATES), None)
        if active is None:
            # Risk 6: a terminal run proves the event was already processed
            # (dedup protects), so treat it as a no-op instead of inventing a
            # completion; anything else is a foreign-session event.
            if runs and runs[0].run_state in _TERMINAL_RUN_STATES:
                return
            self.diagnostics.no_active_run_dropped += 1
            logger.warning(
                "Agent pipeline %s: dropping event %s (%s) for conversation %s with no active run",
                self.binding_id,
                notification.dedup_key,
                notification.kind.value,
                conversation_id,
            )
            return
        run_id = active.id

        # 4) Run state machine on explicit boundaries (idempotent start).
        boundary = AgentRunStateMachine.infer_run_boundary(self._capabilities, notification)
        if boundary is RunBoundary.START:
            try:
                await self.run_machine.start(
                    run_id,
                    backend_run_id=(
                        str(notification.run_id) if notification.run_id is not None else None
                    ),
                )
            except RunStateError:
                # Already running: idempotent redelivery of RUN_STARTED.
                self.diagnostics.run_start_idempotent += 1
        elif boundary is RunBoundary.END:
            try:
                if notification.kind is AgentEventKind.RUN_FAILED:
                    await self.run_machine.fail(
                        run_id,
                        error_code=notification.payload.error_code or "run_failed",
                    )
                else:
                    await self.run_machine.complete(run_id)
            except RunStateError:
                # Out-of-order/redelivered boundary: the event row is still
                # persisted below, but the run cannot transition twice.
                logger.warning(
                    "Agent pipeline %s: run %s could not apply %s (already "
                    "terminal or never started); ignoring the transition",
                    self.binding_id,
                    run_id,
                    notification.kind.value,
                )
            else:
                self._signal_run_terminal(run_id)
                self._run_envelopes.pop(run_id, None)

        # 5) Canonical event persistence (one JSON string for digest + payload).
        payload_json = _canonical_payload_json(notification)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        _, inserted = await self._cursor.append(
            conversation_id=conversation_id,
            event_kind=notification.kind.value,
            dedup_key=notification.dedup_key,
            payload_digest=payload_digest,
            run_id=run_id,
            ephemeral=notification.kind is AgentEventKind.MESSAGE_DELTA,
            payload_json=payload_json,
        )

        # 6) Message assembly: only the durable completed message lands in
        #    agent_messages (MESSAGE_DELTA stays ephemeral, spec §4 step 6).
        #    A redelivered dedup key must not re-assemble or re-publish
        #    (review fix m3): the cursor's inserted flag gates both.
        if notification.kind is AgentEventKind.MESSAGE_COMPLETED:
            if not inserted:
                # Dedup hit: the message was already assembled and published
                # when the row was first inserted.
                return
            text = notification.payload.text
            if text is None:
                logger.warning(
                    "Agent pipeline %s: MESSAGE_COMPLETED %s carried no text; "
                    "no message row was assembled",
                    self.binding_id,
                    notification.dedup_key,
                )
                return
            await self._repositories.agent_messages.create(
                conversation_id=conversation_id,
                role="assistant",
                kind="text",
                body_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                run_id=run_id,
                is_final=True,
            )

    async def _reconcile_and_reconnect(self) -> None:
        """Reconcile non-terminal runs, then resubscribe after bounded backoff."""
        self._reconnect_attempts += 1
        self.diagnostics.reconnects += 1
        # Active runs are queried through the binding join (M4.5 §5): the
        # previous conversation-listing iteration (default limit 50) silently
        # skipped the 51st+ conversation's active runs.
        runs = await self._repositories.agent_runs.list_active_for_binding(
            self.binding_id
        )
        for run in runs:
            ref_row = await self._repositories.agent_backend_conversations.get_by_conversation(
                run.conversation_id
            )
            if ref_row is None:
                continue
            ref = self._to_turns_ref(ref_row)
            snapshot = None
            for attempt in range(self._reconcile_attempts):
                snapshot = await self._adapter.reconcile(ref)
                if snapshot.outcome is not BackendOutcome.UNKNOWN:
                    break
                if attempt + 1 < self._reconcile_attempts:
                    await asyncio.sleep(
                        min(
                            self._reconnect_backoff_base * (2**attempt),
                            self._reconnect_backoff_cap,
                        )
                    )
            if snapshot is None or snapshot.outcome is BackendOutcome.CONFIRMED:
                continue
            current = await self._repositories.agent_runs.get_by_id(run.id)
            if current is None or current.run_state in _TERMINAL_RUN_STATES:
                # A durable boundary event won the race while reconcile was
                # in flight.  Its state is authoritative; do not overwrite it
                # or mislabel the already-known delivery as unknown.
                self._signal_run_terminal(run.id)
                self._run_envelopes.pop(run.id, None)
                continue
            if snapshot.outcome is BackendOutcome.CONTEXT_LOST:
                await self._transition_run_terminal(run.id, "failed", error_code="context_lost")
            else:
                await self._transition_run_terminal(run.id, "unknown")
            await self._park_run_delivery(run.id)
            self._signal_run_terminal(run.id)

        delay = min(
            self._reconnect_backoff_base * (2 ** (self._reconnect_attempts - 1)),
            self._reconnect_backoff_cap,
        )
        logger.warning(
            "Agent pipeline %s: SSE stream ended; reconnecting in %.2fs (disconnect %d)",
            self.binding_id,
            delay,
            self._reconnect_attempts,
        )
        await asyncio.sleep(delay)
