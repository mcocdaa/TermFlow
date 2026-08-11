"""Durable Agent Inbox delivery state machine (plan §7; task M1.4).

Wraps :class:`~termflow_control_plane.persistence.repositories.AgentInboxRepository`
with the plan §7 delivery and submission state machines:

.. code-block:: text

    delivery_state:
    pending -> claimed -> dispatched
                        └-> retry_wait
                        └-> delivery_unknown
                        └-> dead_letter
    pending/claimed/retry_wait -> cancelled

    submission_state:
    not_started -> started -> accepted|rejected|unknown

``dispatched`` means B has submitted the item to the backend, not that the
model turn completed.  Before any socket write the caller commits
``started`` with an ``attempt_id``, stable ``message_id``, owner lease, and
fencing token via :meth:`InboxDeliveryStateMachine.mark_started`.

Auto-retry happens only when submission is provably ``not_started`` or the
backend rejected the stable delivery key.  A network-unknown outcome becomes
``delivery_unknown``; recovery is reconciliation or an explicit user decision,
never an automatic duplicate action.

Persistence mapping (M1.4)
==========================

The M1.3 ``agent_inbox_items`` table (migration ``0006``) stores
``delivery_state``, ``attempt_count``, and claim lease columns only; it has no
``submission_state``, ``attempt_id``, ``stable_message_id``, or
``fencing_token`` columns and the repository has no ``started`` API.  Until a
later milestone adds schema/repository support:

* :meth:`mark_started` persists the delivery transition through the existing
  ``mark_dispatched`` repository call (``claimed`` -> ``dispatched``, lease
  released) and records ``submission_state=started`` plus the attempt ID,
  stable message ID, owner, and fencing token on the in-memory
  :class:`_StartedSubmission` record, exposed on the returned
  :class:`InboxEnvelope`.
* Because ``mark_started`` maps ``started`` onto the ``dispatched`` delivery
  state and the repository has no ``dispatched`` -> ``retry_wait`` path, a
  rejection observed *after* ``mark_started`` cannot be auto-retried in M1.4;
  only never-submitted items retry (``pending``/``claimed``/``retry_wait``).
  ``mark_delivery_unknown`` and ``recover_stale`` remain the honest terminal
  paths for started items until a later milestone adds schema/repository
  support for the submission track.
* ``delivery_unknown`` is represented only in memory
  (:attr:`InboxDeliveryStateMachine._parked`); the repository cannot persist
  it, so :meth:`claim_next` and :meth:`recover_stale` consult that guard to
  keep unknown items out of the claim path.
* :meth:`recover_stale` restores provably-not-started expired claims to
  ``pending`` without a write: the repository treats an expired ``claimed``
  row as reclaimable, which preserves the attempt count exactly.

The repository is the last line of defense: every transition below also
passes through the repository's own ``WHERE`` guards and CAS (``claim``,
``mark_dispatched``).  If the repository rejects a transition, the state
machine raises :class:`InboxStateError` (fail-loud policy, see
:meth:`_guard`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from termflow_control_plane.persistence.models import AgentInboxItem
from termflow_control_plane.persistence.repositories import AgentInboxRepository
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
    ConcurrencyMode,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import BackendOutcome

# Delivery states that count as an in-flight turn for the one-in-flight gate.
_IN_FLIGHT_DELIVERY_STATES = ("dispatched", "cancel_requested")

# Submission states that may be retried automatically.
_RETRYABLE_SUBMISSION_STATES = ("not_started", "rejected")

# Backend outcomes that prove a definitive answer, so an automatic retry
# would risk a duplicate action.
_NON_RETRYABLE_OUTCOMES = (
    BackendOutcome.CONFIRMED,
    BackendOutcome.REQUESTED,
    BackendOutcome.CONTEXT_LOST,
    BackendOutcome.UNKNOWN,
)

# The repository's ``WHERE`` guards for each transition; used to validate
# before calling and to explain failures after the fact.
_ALLOWED_FOR_RETRY = ("pending", "claimed", "retry_wait")
_ALLOWED_FOR_CANCEL = ("pending", "claimed", "retry_wait")
_ALLOWED_FOR_DEAD_LETTER = ("pending", "claimed", "retry_wait")
_ALLOWED_FOR_DISPATCH = ("claimed",)
_ALLOWED_FOR_DELIVERY_UNKNOWN = ("claimed", "dispatched")


class InboxStateError(Exception):
    """Raised when a state-machine transition is illegal or rejected.

    The state machine uses a fail-loud policy: an illegal transition (or a
    transition the repository's CAS rejects) raises instead of silently
    dropping the caller's intent.
    """


@dataclass
class InboxEnvelope:
    """Working representation of one inbox row plus its submission state.

    The B-only :class:`~.turns.AgentInboxEnvelope` describes the wire-facing
    shape; this dataclass is the state machine's mutable working copy and also
    carries the submission fields the M1.3 schema cannot yet persist.
    """

    id: UUID
    conversation_id: UUID
    admission_seq: int
    kind: str
    actor_id: str
    actor_kind: str
    auth_epoch: int | None
    idempotency_key: str
    payload_digest: str
    source: str
    correlation_id: UUID
    causation_id: UUID | None
    delivery_state: str
    submission_state: str = "not_started"
    attempt_count: int = 0
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    attempt_id: str | None = None
    stable_message_id: str | None = None
    fencing_token: str | None = None


@dataclass
class _StartedSubmission:
    """In-memory ``started`` submission record (schema lacks the columns)."""

    attempt_id: str
    stable_message_id: str
    owner: str
    fencing_token: str
    claimed_at: datetime
    lease_expires_at: datetime
    # Snapshot of the envelope at the moment submission started, so recovery
    # can report the item without another query.
    snapshot: InboxEnvelope
    submission_state: str = "started"


class InboxDeliveryStateMachine:
    """Durable claiming, delivery, retry, and recovery for the Agent Inbox.

    ``now`` is a time-source callable (defaults to ``datetime.now(UTC)``) so
    claim leases, retry deadlines, and stale recovery are deterministic in
    tests.  ``lease_seconds`` bounds each claim; ``max_attempts`` bounds
    delivery attempts before dead-lettering; ``max_in_flight_per_conversation``
    (default ``1``) enforces the one-in-flight turn rule unless the backend
    capability advertises ``ConcurrencyMode.PARALLEL``.  ``worker_id``
    prefixes the per-claim fencing token so concurrent workers never collide.
    """

    def __init__(
        self,
        agent_inbox: AgentInboxRepository,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        max_in_flight_per_conversation: int = 1,
        capabilities: AgentBackendCapabilities | None = None,
        worker_id: str = "worker",
    ) -> None:
        self._agent_inbox = agent_inbox
        self._clock = now or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._max_in_flight_per_conversation = max_in_flight_per_conversation
        self._capabilities = capabilities
        self._worker_id = worker_id
        # item_id -> delivery state parked in memory because the repository
        # cannot persist delivery_unknown (or proven-accepted recovery).
        self._parked: dict[UUID, str] = {}
        # item_id -> started submission metadata (schema lacks the columns).
        self._submissions: dict[UUID, _StartedSubmission] = {}

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    async def claim_next(
        self,
        *,
        conversation_id: UUID | None = None,
    ) -> InboxEnvelope | None:
        """Claim the next eligible item using ``next_pending`` + CAS ``claim``.

        Enforces at most one in-flight turn per conversation (unless the
        backend capability allows concurrency) and never re-claims an item
        parked as ``delivery_unknown`` or proven accepted.
        """
        observed = self._now()
        candidates = [
            row
            for row in await self._agent_inbox.next_pending(
                conversation_id=conversation_id,
                now=observed,
            )
            if row.id not in self._parked
        ]
        if not candidates:
            return None

        limit = self._in_flight_limit()
        if limit is not None:
            counts: dict[UUID, int] = {}
            eligible: list[AgentInboxItem] = []
            for row in candidates:
                if row.conversation_id not in counts:
                    counts[row.conversation_id] = await self._in_flight_count(
                        row.conversation_id,
                        now=observed,
                    )
                if counts[row.conversation_id] < limit:
                    eligible.append(row)
            candidates = eligible

        for row in candidates:
            owner = f"{self._worker_id}:{row.admission_seq}:{uuid4().hex[:8]}"
            claimed = await self._agent_inbox.claim(
                row.id,
                owner,
                lease_seconds=self._lease_seconds,
                now=observed,
            )
            if claimed is None:
                # Lost the CAS race to another worker; try the next candidate.
                continue
            return self._envelope(claimed)
        return None

    def _in_flight_limit(self) -> int | None:
        """Concurrency allowance for one conversation (``None`` = unbounded)."""
        if (
            self._capabilities is not None
            and self._capabilities.concurrency_mode is ConcurrencyMode.PARALLEL
        ):
            return None
        return self._max_in_flight_per_conversation

    async def _in_flight_count(self, conversation_id: UUID, *, now: datetime) -> int:
        rows = await self._agent_inbox.list_for_conversation(conversation_id)
        return sum(1 for row in rows if self._is_in_flight(row, now=now))

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        """Normalize SQLite round-tripped datetimes back to aware UTC.

        SQLite stores datetimes without a timezone, so the repository returns
        naive datetimes; attach UTC so Python-side comparisons never mix
        aware and naive values.
        """
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _is_in_flight(row: AgentInboxItem, *, now: datetime) -> bool:
        if row.delivery_state in _IN_FLIGHT_DELIVERY_STATES:
            return True
        claim_expires_at = InboxDeliveryStateMachine._aware(row.claim_expires_at)
        return (
            row.delivery_state == "claimed"
            and claim_expires_at is not None
            and claim_expires_at > now
        )

    # ------------------------------------------------------------------
    # Submission and delivery transitions
    # ------------------------------------------------------------------

    async def mark_started(
        self,
        envelope: InboxEnvelope,
        *,
        attempt_id: str,
        stable_message_id: str,
        owner: str,
        fencing_token: str,
    ) -> InboxEnvelope:
        """Commit ``submission_state=started`` before any network write.

        Persists the delivery transition via the repository's
        ``mark_dispatched`` (``claimed`` -> ``dispatched``, lease released)
        and records the attempt ID / stable message ID / owner / fencing token
        on the in-memory submission record, surfaced on the returned envelope.
        """
        observed = self._now()
        self._guard(envelope, _ALLOWED_FOR_DISPATCH, "dispatch")
        if envelope.claim_owner is None or envelope.claim_owner != owner:
            raise InboxStateError(
                f"cannot start submission of inbox item {envelope.id}: "
                f"owner {owner!r} does not hold the claim "
                f"(claim_owner={envelope.claim_owner!r})"
            )
        if envelope.claim_expires_at is None or envelope.claim_expires_at <= observed:
            raise InboxStateError(
                f"cannot start submission of inbox item {envelope.id}: "
                "claim lease is missing or has expired"
            )
        dispatched = await self._agent_inbox.mark_dispatched(
            envelope.id,
            owner,
            now=observed,
        )
        if dispatched is None:
            raise InboxStateError(
                f"cannot start submission of inbox item {envelope.id}: "
                "repository rejected the claimed -> dispatched transition"
            )

        lease_expires_at = envelope.claim_expires_at
        self._submissions[envelope.id] = _StartedSubmission(
            attempt_id=attempt_id,
            stable_message_id=stable_message_id,
            owner=owner,
            fencing_token=fencing_token,
            claimed_at=lease_expires_at - timedelta(seconds=self._lease_seconds),
            lease_expires_at=lease_expires_at,
            snapshot=replace(envelope),
        )
        envelope.delivery_state = "dispatched"
        envelope.submission_state = "started"
        envelope.attempt_id = attempt_id
        envelope.stable_message_id = stable_message_id
        envelope.fencing_token = fencing_token
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        return envelope

    async def mark_dispatched(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """Fence the dispatch on the live claim (thin repository wrapper)."""
        observed = self._now()
        self._guard(envelope, _ALLOWED_FOR_DISPATCH, "dispatch")
        if envelope.claim_owner is None:
            raise InboxStateError(
                f"cannot dispatch inbox item {envelope.id}: no claim owner"
            )
        dispatched = await self._agent_inbox.mark_dispatched(
            envelope.id,
            envelope.claim_owner,
            now=observed,
        )
        if dispatched is None:
            raise InboxStateError(
                f"cannot dispatch inbox item {envelope.id}: "
                "repository rejected the claimed -> dispatched transition"
            )
        envelope.delivery_state = "dispatched"
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        return envelope

    async def mark_retry(
        self,
        envelope: InboxEnvelope,
        *,
        backoff: timedelta,
    ) -> InboxEnvelope:
        """Retry a failed delivery, dead-lettering at the attempt cap.

        Only items that have not been submitted (``pending``/``claimed``/
        ``retry_wait``) can retry: the M1.4 repository has no path from
        ``dispatched`` back to ``retry_wait``, so a rejection observed after
        :meth:`mark_started` must be reconciled rather than auto-retried
        (see the module docstring for the persistence mapping).
        """
        observed = self._now()
        self._guard(envelope, _ALLOWED_FOR_RETRY, "retry")
        if envelope.attempt_count + 1 >= self._max_attempts:
            return await self.dead_letter(envelope)

        next_attempt_at = observed + backoff
        retried = await self._agent_inbox.mark_retry(
            envelope.id,
            next_attempt_at=next_attempt_at,
        )
        if retried is None:
            raise InboxStateError(
                f"cannot retry inbox item {envelope.id}: "
                "repository rejected the retry_wait transition"
            )
        envelope.delivery_state = "retry_wait"
        envelope.attempt_count = retried.attempt_count
        envelope.next_attempt_at = next_attempt_at
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        return envelope

    async def mark_delivery_unknown(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """Park the item as ``delivery_unknown`` (never auto-retried).

        The repository cannot persist this state (M1.4 limitation), so the
        state machine keeps the item parked in memory and both
        :meth:`claim_next` and :meth:`recover_stale` exclude it from the claim
        path.  Recovery is reconciliation or an explicit user decision.
        """
        self._guard(envelope, _ALLOWED_FOR_DELIVERY_UNKNOWN, "mark delivery_unknown")
        self._parked[envelope.id] = "delivery_unknown"
        self._record_outcome(envelope.id, "unknown")
        envelope.delivery_state = "delivery_unknown"
        envelope.submission_state = "unknown"
        return envelope

    async def dead_letter(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """Move the item to ``dead_letter`` (bounded attempts exhausted)."""
        self._guard(envelope, _ALLOWED_FOR_DEAD_LETTER, "dead-letter")
        dead = await self._agent_inbox.dead_letter(envelope.id)
        if dead is None:
            raise InboxStateError(
                f"cannot dead-letter inbox item {envelope.id}: "
                "repository rejected the dead_letter transition"
            )
        envelope.delivery_state = "dead_letter"
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        envelope.next_attempt_at = None
        return envelope

    async def mark_cancelled(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """Cancel a queued/claimed/retrying item (never a dispatched one)."""
        self._guard(envelope, _ALLOWED_FOR_CANCEL, "cancel")
        cancelled = await self._agent_inbox.mark_cancelled(envelope.id)
        if cancelled is None:
            raise InboxStateError(
                f"cannot cancel inbox item {envelope.id}: "
                "repository rejected the cancelled transition"
            )
        envelope.delivery_state = "cancelled"
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        envelope.next_attempt_at = None
        return envelope

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover_stale(
        self,
        *,
        now: datetime,
        reconciled_ids: set[UUID] | None = None,
    ) -> list[InboxEnvelope]:
        """Recover expired claims after a crash or long outage.

        * Expired claims that never reached ``started`` are restored to
          ``pending`` and may be re-claimed (provably not started).
        * Expired ``started`` submissions become ``delivery_unknown`` unless
          the item is in ``reconciled_ids``, in which case reconciliation has
          proven the outcome and the item is restored to ``dispatched``
          (accepted) and never re-offered for delivery.
        """
        reconciled = reconciled_ids or set()
        recovered: list[InboxEnvelope] = []

        # 1) Expired claims the database can still see: by construction these
        #    never reached `started` (mark_started persists `dispatched`).
        for row in await self._agent_inbox.next_pending(now=now):
            if row.delivery_state != "claimed" or row.id in self._parked:
                continue
            envelope = self._envelope(row)
            if row.id in reconciled:
                self._parked[row.id] = "dispatched"
                envelope.delivery_state = "dispatched"
                envelope.submission_state = "accepted"
            else:
                # Restore to pending without a write: an expired claimed row
                # is already reclaimable, and attempts are preserved.
                envelope.delivery_state = "pending"
            recovered.append(envelope)

        # 2) Started submissions whose claim lease expired without a proven
        #    outcome (their database row is already `dispatched`).
        for item_id, submission in list(self._submissions.items()):
            if submission.submission_state != "started":
                continue
            if submission.lease_expires_at > now or item_id in self._parked:
                continue
            envelope = replace(submission.snapshot)
            if item_id in reconciled:
                submission.submission_state = "accepted"
                self._parked[item_id] = "dispatched"
                envelope.delivery_state = "dispatched"
            else:
                submission.submission_state = "unknown"
                self._parked[item_id] = "delivery_unknown"
                envelope.delivery_state = "delivery_unknown"
            envelope.submission_state = submission.submission_state
            recovered.append(envelope)

        return recovered

    # ------------------------------------------------------------------
    # Pure policy helpers
    # ------------------------------------------------------------------

    @staticmethod
    def should_auto_retry(*, submission_state: str, outcome: BackendOutcome | str) -> bool:
        """Whether an item may be retried without risking a duplicate action.

        True only when submission is provably not started, or the backend
        rejected the stable delivery key (a definitive rejection).  Any
        unknown/context-lost outcome is never retried automatically.
        """
        if submission_state not in _RETRYABLE_SUBMISSION_STATES:
            return False
        if submission_state == "not_started":
            return True
        return outcome not in _NON_RETRYABLE_OUTCOMES

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _guard(self, envelope: InboxEnvelope, allowed: tuple[str, ...], verb: str) -> None:
        if envelope.delivery_state not in allowed:
            raise InboxStateError(
                f"cannot {verb} inbox item {envelope.id} from "
                f"delivery_state={envelope.delivery_state!r}; "
                f"allowed: {', '.join(allowed)}"
            )

    def _record_outcome(self, item_id: UUID, outcome: str) -> None:
        if item_id in self._submissions:
            self._submissions[item_id].submission_state = outcome

    def _envelope(self, row: AgentInboxItem) -> InboxEnvelope:
        submission = self._submissions.get(row.id)
        return InboxEnvelope(
            id=row.id,
            conversation_id=row.conversation_id,
            admission_seq=row.admission_seq,
            kind=row.kind,
            actor_id=row.actor_id,
            actor_kind=row.actor_kind,
            auth_epoch=row.auth_epoch,
            idempotency_key=row.idempotency_key,
            payload_digest=row.payload_digest,
            source=row.source,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            delivery_state=row.delivery_state,
            submission_state=(
                submission.submission_state if submission is not None else "not_started"
            ),
            attempt_count=row.attempt_count,
            claim_owner=row.claim_owner,
            claim_expires_at=self._aware(row.claim_expires_at),
            next_attempt_at=self._aware(row.next_attempt_at),
            attempt_id=submission.attempt_id if submission is not None else None,
            stable_message_id=(
                submission.stable_message_id if submission is not None else None
            ),
            fencing_token=submission.fencing_token if submission is not None else None,
        )
