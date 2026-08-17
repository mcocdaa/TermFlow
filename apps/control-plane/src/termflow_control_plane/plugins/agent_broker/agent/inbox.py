"""Durable Agent Inbox delivery state machine (plan §7; task M1.4).

Wraps :class:`~termflow_control_plane.persistence.repositories.AgentInboxRepository`
with the plan §7 delivery and submission state machines:

.. code-block:: text

    delivery_state:
    pending -> claimed -> dispatched -> delivered (proven accepted)
                        \\-> retry_wait
                        \\-> delivery_unknown (persisted; never auto-retried)
                        \\-> dead_letter
                        \\-> cancel_requested -> cancelled|delivery_unknown
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

Persistence mapping
===================

Terminal delivery is durable, not in-memory:

* ``mark_started`` commits the dispatch through the repository's
  ``start_submission`` CAS (``claimed`` -> ``dispatched``): the started row
  keeps the claim lease as its submission lease and persists the
  attempt/fencing identity as ``claim_owner = "<attempt_id>|<fencing_token>"``.
* When a run reaches a terminal outcome the pipeline proves the delivery:
  ``mark_delivered`` persists ``dispatched -> delivered`` (submission
  ``accepted``), which releases the one-in-flight gate so a conversation can
  run its next turn (plan §2.1 multi-turn conversations).
* ``mark_delivery_unknown`` persists the terminal state through the
  repository (no in-memory ``_parked`` guard): the claim scan never re-offers
  an unknown delivery, and restart recovery sees it as visible, recoverable
  state (plan §7/§17).
* Cancellation follows the plan path ``dispatched -> cancel_requested ->
  cancelled|delivery_unknown``; the submission lease is kept through the
  cancel round trip so recovery can fence a cancel that died mid-flight.
* :meth:`recover_stale` fences from the database only: expired never-started
  claims are restorable to ``pending`` (provably not started), while expired
  ``started``/``cancel_requested`` submissions become ``delivered`` (when
  reconciliation proves acceptance) or ``delivery_unknown``.

The repository is the last line of defense: every transition below also
passes through the repository's own ``WHERE`` guards and CAS (``claim``,
``start_submission``, ``mark_delivered``, ...).  If the repository rejects a
transition, the state machine raises :class:`InboxStateError` (fail-loud
policy, see :meth:`_guard`).
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
_ALLOWED_FOR_CANCEL = ("pending", "claimed", "retry_wait", "cancel_requested")
_ALLOWED_FOR_DEAD_LETTER = ("pending", "claimed", "retry_wait")
_ALLOWED_FOR_DISPATCH = ("claimed",)
_ALLOWED_FOR_DELIVERY_UNKNOWN = ("claimed", "dispatched", "cancel_requested")
_ALLOWED_FOR_DELIVERED = ("dispatched",)
_ALLOWED_FOR_CANCEL_REQUEST = ("dispatched",)

# Bounded keyset scan for per-conversation claiming (review fix major-1): a
# page of ``next_pending`` candidates fully blocked by the claim filter must
# not starve later rows, so the scan advances past it - up to this many
# pages (10 rows each) per claim attempt.
_CLAIM_SCAN_MAX_PAGES = 10


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
    """Live ``started`` submission record for the current process.

    The durable counterpart (attempt/fencing identity and submission lease)
    is persisted on the inbox row by the repository's ``start_submission``;
    this record only enriches live envelopes until the process restarts.
    """

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
    ``claim_filter`` (optional) is a caller-supplied eligibility predicate
    applied to every claim candidate: a candidate it rejects is skipped
    without consuming a claim, so it stays visible in its current state
    (the M4.5 watch sink uses it to wait for the typed payload).
    ``claim_scan_max_pages`` bounds the keyset candidate scan (review fix
    major-1) so a window fully blocked by the filter can never hang the
    claim attempt on an unbounded walk.
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
        claim_filter: Callable[[AgentInboxItem], bool] | None = None,
        claim_scan_max_pages: int = _CLAIM_SCAN_MAX_PAGES,
    ) -> None:
        if claim_scan_max_pages < 1:
            raise ValueError("claim_scan_max_pages must be at least 1")
        self._agent_inbox = agent_inbox
        self._clock = now or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._max_in_flight_per_conversation = max_in_flight_per_conversation
        self._capabilities = capabilities
        self._worker_id = worker_id
        self._claim_filter = claim_filter
        self._claim_scan_max_pages = claim_scan_max_pages
        # item_id -> started submission metadata for live envelopes (the
        # durable counterpart lives on the inbox row: attempt/fencing identity
        # and submission lease persist through ``start_submission``).
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
        whose delivery already reached a persisted terminal state
        (``delivered``/``delivery_unknown``/...): those rows are excluded by
        the repository's claimable-state filter.

        Candidates are scanned in bounded keyset pages (review fix
        major-1): a page fully rejected by the claim filter no longer
        starves later rows, because the scan advances past it up to
        ``claim_scan_max_pages`` pages.
        """
        observed = self._now()
        cursor: tuple[UUID, int] | None = None
        for _ in range(self._claim_scan_max_pages):
            rows = await self._agent_inbox.next_pending(
                conversation_id=conversation_id,
                now=observed,
                after=cursor,
            )
            if not rows:
                return None
            cursor = (rows[-1].conversation_id, rows[-1].admission_seq)
            candidates = [
                row
                for row in rows
                if self._claim_filter is None or self._claim_filter(row)
            ]
            if not candidates:
                # The page is fully blocked by the parked/claim filters:
                # advance past it instead of starving the rows behind it.
                continue

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
            if not candidates:
                # Every page candidate exceeds the in-flight gate; scanning
                # further pages cannot lift it.
                return None

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
            # Every candidate lost the CAS race; the next poll picks up the
            # rest of the page.
            return None
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

        Persists the delivery transition through the repository's
        ``start_submission`` CAS (``claimed`` -> ``dispatched``): the started
        row keeps a submission lease and persists the attempt/fencing
        identity in the claim owner column, so restart recovery can fence the
        submission without in-memory state (plan §7/§17).
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
        started = await self._agent_inbox.start_submission(
            envelope.id,
            owner,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            lease_seconds=self._lease_seconds,
            now=observed,
        )
        if started is None:
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
        """Fence the dispatch on the live claim (thin repository wrapper).

        The repository keeps the claim owner/lease on the dispatched row so
        the submission stays recovery-fenceable (plan §17): a dispatched row
        whose outcome is never proven can be surfaced as ``delivery_unknown``
        by a later recovery sweep instead of blocking the gate forever.
        """
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

    async def mark_delivered(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """Prove the delivery accepted: ``dispatched`` -> ``delivered``.

        The persisted terminal state releases the one-in-flight gate, so the
        conversation's next turn can be claimed once the current run has
        terminated (plan §2.1 multi-turn conversations; submission
        ``accepted``).
        """
        self._guard(envelope, _ALLOWED_FOR_DELIVERED, "mark delivered")
        delivered = await self._agent_inbox.mark_delivered(envelope.id)
        if delivered is None:
            raise InboxStateError(
                f"cannot mark inbox item {envelope.id} delivered: "
                "repository rejected the dispatched -> delivered transition"
            )
        self._record_outcome(envelope.id, "accepted")
        envelope.delivery_state = "delivered"
        envelope.submission_state = "accepted"
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        envelope.next_attempt_at = None
        return envelope

    async def mark_cancel_requested(self, envelope: InboxEnvelope) -> InboxEnvelope:
        """``dispatched`` -> ``cancel_requested`` before the backend cancel call.

        The item stays in-flight during the cancel round trip; the terminal
        outcome is ``cancelled`` or ``delivery_unknown`` (plan §7).  The
        submission lease is kept so recovery can fence a cancel that died
        mid-flight.
        """
        self._guard(envelope, _ALLOWED_FOR_CANCEL_REQUEST, "mark cancel_requested")
        requested = await self._agent_inbox.mark_cancel_requested(envelope.id)
        if requested is None:
            raise InboxStateError(
                f"cannot mark inbox item {envelope.id} cancel_requested: "
                "repository rejected the dispatched -> cancel_requested transition"
            )
        envelope.delivery_state = "cancel_requested"
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
        """Persist the item as ``delivery_unknown`` (never auto-retried).

        The repository CAS persists the terminal state, so the claim path and
        restart recovery see it without any in-memory guard (plan §7/§17:
        uncertain delivery is a visible recoverable state).  Recovery is
        reconciliation or an explicit user decision.
        """
        self._guard(envelope, _ALLOWED_FOR_DELIVERY_UNKNOWN, "mark delivery_unknown")
        unknown = await self._agent_inbox.mark_delivery_unknown(envelope.id)
        if unknown is None:
            raise InboxStateError(
                f"cannot mark inbox item {envelope.id} delivery_unknown: "
                "repository rejected the transition"
            )
        self._record_outcome(envelope.id, "unknown")
        envelope.delivery_state = "delivery_unknown"
        envelope.submission_state = "unknown"
        envelope.claim_owner = None
        envelope.claim_expires_at = None
        envelope.next_attempt_at = None
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
        """Cancel a queued/claimed/retrying item, or finish a cancel request.

        The plan §7 cancel path is ``dispatched -> cancel_requested ->
        cancelled``: a ``cancel_requested`` item whose backend cancellation
        was proven terminal lands here; an unproven one becomes
        ``delivery_unknown`` instead.
        """
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
        """Recover expired claims and unproven started submissions after a crash.

        * Expired claims that never reached ``started`` are restored to
          ``pending`` and may be re-claimed (provably not started).
        * Expired ``started``/``cancel_requested`` submissions are fenced
          from the database row (review fix: the submission lease and
          attempt/fencing identity are persisted, so recovery needs no
          in-memory state): items in ``reconciled_ids`` have a proven outcome
          and become ``delivered`` (accepted); everything else becomes the
          persisted terminal ``delivery_unknown`` and is never re-offered for
          delivery.
        """
        reconciled = reconciled_ids or set()
        recovered: list[InboxEnvelope] = []

        # 1) Expired claims the database can still see: by construction these
        #    never reached `started` (start_submission persists `dispatched`).
        for row in await self._agent_inbox.next_pending(now=now):
            if row.delivery_state != "claimed":
                continue
            envelope = self._envelope(row)
            if row.id in reconciled:
                envelope.delivery_state = "dispatched"
                envelope.submission_state = "accepted"
            else:
                # Restore to pending without a write: an expired claimed row
                # is already reclaimable, and attempts are preserved.
                envelope.delivery_state = "pending"
            recovered.append(envelope)

        # 2) Started submissions whose submission lease expired without a
        #    proven outcome (their database row is `dispatched` or
        #    `cancel_requested` and still carries the persisted lease).
        for row in await self._agent_inbox.list_started_with_expired_lease(now=now):
            attempt_id, fencing_token = self._started_identity(row.claim_owner)
            if row.id in reconciled:
                terminal = await self._agent_inbox.mark_delivered(row.id)
                if terminal is None:
                    # Lost the CAS to another recovery sweep; its result
                    # already covers this item.
                    continue
                submission_state = "accepted"
            else:
                terminal = await self._agent_inbox.mark_delivery_unknown(row.id)
                if terminal is None:
                    continue
                submission_state = "unknown"
            envelope = self._envelope(terminal)
            envelope.submission_state = submission_state
            envelope.attempt_id = attempt_id
            envelope.stable_message_id = terminal.idempotency_key
            envelope.fencing_token = fencing_token
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

    @staticmethod
    def _started_identity(claim_owner: str | None) -> tuple[str | None, str | None]:
        """Split the persisted ``<attempt_id>|<fencing_token>`` claim identity.

        Rows dispatched through the plain :meth:`mark_dispatched` path keep
        the worker owner string (no ``|``), which doubles as the attempt
        identity for recovery reporting.
        """
        if not claim_owner:
            return None, None
        attempt, separator, fencing = claim_owner.partition("|")
        if not separator:
            return claim_owner, None
        return attempt or None, fencing or None

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
