"""Assisted-write approval workflow and canonical arguments hash (plan §12.1, task M5.1).

Every assisted write request becomes a persistent, single-use
:class:`~termflow_control_plane.persistence.models.ApprovalRequest` whose
lifecycle follows plan §12.1:

.. code-block:: text

    pending -> approved|denied|expired|revoked|consumed|unknown

A decision (:meth:`ApprovalPolicy.decide`) is an atomic compare-and-swap from
``pending`` to ``approved``/``denied`` executed in a single SQL statement that
binds the caller's auth epoch and the request expiry to the row itself:

- the caller's auth epoch must equal the epoch recorded when the request was
  created, so a stale UI replay or a decision made after an auth epoch change
  can never win;
- a pending-but-time-expired request and a swept ``expired`` request are both
  rejected;
- a double decision loses the CAS and is rejected, and the first decision is
  never overwritten.

The CAS cannot be expressed through the ``ApprovalRepository`` (its
``set_state`` is not epoch-bound), so the transitions execute directly against
the session factory.  ``actor`` is accepted by every user-driven transition
and validated, ready for the M5.3 audit wiring (no actor column exists yet).

:func:`canonical_hash` deterministically binds an approval to the exact write:
schema version, operation, instance/pane incarnation, encoded text/key bytes,
submit flag, cursor precondition, run/grant id, expiry, and policy epoch.
Any change to any input changes the hash, so an approval can never authorize a
different write than the one that was reviewed.  The encoding is locked by an
explicit test vector in ``tests/test_agent_approvals.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_protocol.agent import ApprovalDecision

from termflow_control_plane.persistence.models import ApprovalRequest
from termflow_control_plane.persistence.repositories import RepositoryBundle

_CANONICAL_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_TOOL_CALL_ID_LENGTH = 128
_MAX_ACTOR_LENGTH = 256


class ApprovalState(StrEnum):
    """Persistent lifecycle states of an approval request (plan §12.1)."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"
    UNKNOWN = "unknown"


class ApprovalError(RuntimeError):
    """Base class for approval workflow rejections."""


class ApprovalNotFound(ApprovalError):
    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval request {approval_id} does not exist")


class ApprovalExpired(ApprovalError):
    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval request {approval_id} has expired")


class ApprovalRevoked(ApprovalError):
    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval request {approval_id} has been revoked")


class ApprovalAlreadyDecided(ApprovalError):
    """The request is in a state that cannot be decided or consumed again."""

    def __init__(self, approval_id: UUID, *, state: str, decision: str | None) -> None:
        self.approval_id = approval_id
        self.state = state
        self.decision = decision
        super().__init__(
            f"approval request {approval_id} is in state {state!r} "
            f"(decision={decision!r}) and cannot transition again"
        )


class ApprovalAuthEpochStale(ApprovalError):
    """The deciding session's auth epoch does not match the request's epoch."""

    def __init__(self, approval_id: UUID, *, expected: int, actual: int) -> None:
        self.approval_id = approval_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"approval request {approval_id} was created in auth epoch {expected} "
            f"but the deciding session is in epoch {actual}"
        )


class ApprovalAlreadyConsumed(ApprovalError):
    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval request {approval_id} has already been consumed")


class ApprovalToolCallConflict(ApprovalError):
    """A tool call can only ever produce one approval per conversation."""

    def __init__(self, conversation_id: UUID, tool_call_id: str) -> None:
        self.conversation_id = conversation_id
        self.tool_call_id = tool_call_id
        super().__init__(
            f"an approval already exists for tool call {tool_call_id!r} "
            f"in conversation {conversation_id}"
        )


@dataclass(frozen=True, slots=True)
class ApprovalArgsHashInput:
    """Every input that defines one exact write request (plan §12.1).

    ``encoded_bytes`` is the exact text/key payload bytes the write would
    send, ``pane_incarnation`` is the target pane's incarnation at request
    time, and ``cursor_precondition`` is the expected pane cursor before the
    write executes.  ``run_id`` and ``grant_id`` are mutually exclusive in
    practice (a write is either run-scoped or grant-scoped); both are encoded
    so the hash covers whichever identity applies.
    """

    schema_version: int
    operation: str
    instance_id: str | UUID
    pane_id: str
    pane_incarnation: str
    encoded_bytes: bytes
    submit: bool
    cursor_precondition: str | None
    run_id: str | UUID | None
    grant_id: str | None
    expiry: datetime
    policy_epoch: int


def canonical_hash(args: ApprovalArgsHashInput) -> str:
    """Deterministic SHA-256 over the canonical approval arguments.

    The payload is a sort-keyed, separator-stable JSON document so identical
    inputs always produce identical bytes and therefore the same hash, while
    any field change produces a different hash.  ``encoded_bytes`` is encoded
    as hex (stable across binary data) and ``expiry`` must be timezone-aware
    so its ISO-8601 representation is deterministic.
    """
    if args.expiry.tzinfo is None:
        raise ValueError("approval expiry must be timezone-aware")
    if args.schema_version < 1:
        raise ValueError("approval schema_version must be positive")
    payload = json.dumps(
        {
            "schema_version": args.schema_version,
            "operation": args.operation,
            "instance_id": str(args.instance_id),
            "pane_id": args.pane_id,
            "pane_incarnation": args.pane_incarnation,
            "encoded_bytes": args.encoded_bytes.hex(),
            "submit": args.submit,
            "cursor_precondition": args.cursor_precondition,
            "run_id": None if args.run_id is None else str(args.run_id),
            "grant_id": args.grant_id,
            "expiry": args.expiry.isoformat(),
            "policy_epoch": args.policy_epoch,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ApprovalPolicy:
    """Assisted-write approval workflow (plan §12.1).

    Every transition is a database CAS, so concurrent decisions, stale UI
    replays, and double consumption can never win twice.  Rejections raise a
    specific :class:`ApprovalError` subclass that maps to a stable HTTP code
    at the API boundary.
    """

    def __init__(
        self,
        repositories: RepositoryBundle,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repositories = repositories
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_approval(
        self,
        *,
        binding_id: UUID,
        conversation_id: UUID,
        tool_call_id: str,
        canonical_hash: str,
        auth_epoch: int,
        expires_at: datetime,
        run_id: UUID | None = None,
    ) -> ApprovalRequest:
        """Create a pending single-use approval for one write request."""
        if _CANONICAL_HASH_PATTERN.fullmatch(canonical_hash) is None:
            raise ValueError("canonical_hash must be a 64-character hex SHA-256 digest")
        if not tool_call_id or len(tool_call_id) > _MAX_TOOL_CALL_ID_LENGTH:
            raise ValueError("tool_call_id must be 1-128 characters")
        if auth_epoch < 1:
            raise ValueError("auth_epoch must be positive")
        if expires_at <= self._clock():
            raise ValueError("expires_at must be in the future")
        try:
            return await self._repositories.approvals.create(
                binding_id=binding_id,
                conversation_id=conversation_id,
                tool_call_id=tool_call_id,
                canonical_hash=canonical_hash,
                auth_epoch=auth_epoch,
                expires_at=expires_at,
                run_id=run_id,
                state=ApprovalState.PENDING.value,
            )
        except IntegrityError as exc:
            # The unique (conversation_id, tool_call_id) constraint means one
            # tool call can only ever request one approval; a duplicate is a
            # replayed tool call.  FK violations are the caller's contract
            # violation (B always creates approvals for entities it owns).
            raise ApprovalToolCallConflict(conversation_id, tool_call_id) from exc

    async def decide(
        self,
        approval_id: UUID,
        *,
        decision: ApprovalDecision,
        actor: str,
        auth_epoch: int,
        now: datetime | None = None,
    ) -> ApprovalState:
        """Atomically decide a pending request (CAS bound to auth epoch).

        Returns the new state (``approved``/``denied``) or raises an
        :class:`ApprovalError` subclass: ``ApprovalNotFound``,
        ``ApprovalExpired``, ``ApprovalRevoked``, ``ApprovalAlreadyDecided``,
        or ``ApprovalAuthEpochStale``.
        """
        if decision not in (ApprovalDecision.APPROVED, ApprovalDecision.DENIED):
            raise ValueError("a user decision must be approved or denied")
        self._require_actor(actor)
        if auth_epoch < 1:
            raise ValueError("auth_epoch must be positive")
        observed_at = now or self._clock()
        new_state = (
            ApprovalState.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ApprovalState.DENIED
        )
        async with self._sessions() as session:
            result = await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.state == ApprovalState.PENDING.value,
                    ApprovalRequest.auth_epoch == auth_epoch,
                    ApprovalRequest.expires_at > observed_at,
                )
                .values(
                    state=new_state.value,
                    decided_at=observed_at,
                    decision=decision.value,
                )
                .returning(ApprovalRequest.state)
            )
            if result.scalar_one_or_none() is not None:
                await session.commit()
                return new_state
            approval = await session.get(ApprovalRequest, approval_id)
            await session.commit()
            if approval is None:
                raise ApprovalNotFound(approval_id)
            stored_expiry = self._aware(approval.expires_at)
            if stored_expiry is None or stored_expiry <= observed_at:
                raise ApprovalExpired(approval_id)
            if approval.state == ApprovalState.EXPIRED.value:
                raise ApprovalExpired(approval_id)
            if approval.state == ApprovalState.REVOKED.value:
                raise ApprovalRevoked(approval_id)
            if approval.state == ApprovalState.PENDING.value:
                raise ApprovalAuthEpochStale(
                    approval_id,
                    expected=approval.auth_epoch,
                    actual=auth_epoch,
                )
            raise ApprovalAlreadyDecided(
                approval_id,
                state=approval.state,
                decision=approval.decision,
            )

    async def revoke(
        self,
        approval_id: UUID,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> ApprovalState:
        """Revoke a pending or approved request (single CAS, no epoch binding)."""
        self._require_actor(actor)
        observed_at = now or self._clock()
        return await self._transition(
            approval_id,
            new_state=ApprovalState.REVOKED,
            observed_at=observed_at,
            where_states=(ApprovalState.PENDING, ApprovalState.APPROVED),
        )

    async def revoke_for_binding(self, binding_id: UUID, *, now: datetime | None = None) -> int:
        """Revoke every pending/approved request of a binding; returns the count."""
        observed_at = now or self._clock()
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ApprovalRequest)
                    .where(
                        ApprovalRequest.binding_id == binding_id,
                        ApprovalRequest.state.in_(
                            (ApprovalState.PENDING.value, ApprovalState.APPROVED.value)
                        ),
                    )
                    .values(
                        state=ApprovalState.REVOKED.value,
                        decided_at=observed_at,
                        decision=ApprovalState.REVOKED.value,
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count

    async def consume(self, approval_id: UUID, *, now: datetime | None = None) -> ApprovalState:
        """Mark a pending or approved request consumed (single use).

        Called after a terminal A execution outcome: the approval has
        authorized exactly one write and can never authorize another.
        """
        observed_at = now or self._clock()
        return await self._transition(
            approval_id,
            new_state=ApprovalState.CONSUMED,
            observed_at=observed_at,
            where_states=(ApprovalState.PENDING, ApprovalState.APPROVED),
        )

    async def mark_unknown(
        self, approval_id: UUID, *, now: datetime | None = None
    ) -> ApprovalState:
        """Mark an approved request ``unknown`` after an uncertain A outcome.

        The outcome could not be proven, so the approval is never
        automatically replayed: neither a second decision nor a consume can
        move it again.
        """
        observed_at = now or self._clock()
        return await self._transition(
            approval_id,
            new_state=ApprovalState.UNKNOWN,
            observed_at=observed_at,
            where_states=(ApprovalState.APPROVED,),
        )

    async def expire_pending(self, *, now: datetime | None = None) -> int:
        """Sweep pending requests whose expiry passed; returns the count."""
        return await self._repositories.approvals.expire_pending(now=now)

    async def _transition(
        self,
        approval_id: UUID,
        *,
        new_state: ApprovalState,
        observed_at: datetime,
        where_states: tuple[ApprovalState, ...],
    ) -> ApprovalState:
        """Shared CAS core for revoke/consume/mark_unknown with rejection classification."""
        async with self._sessions() as session:
            result = await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.state.in_(
                        tuple(state.value for state in where_states)
                    ),
                )
                .values(
                    state=new_state.value,
                    decided_at=observed_at,
                    decision=new_state.value,
                )
                .returning(ApprovalRequest.state)
            )
            if result.scalar_one_or_none() is not None:
                await session.commit()
                return new_state
            approval = await session.get(ApprovalRequest, approval_id)
            await session.commit()
            if approval is None:
                raise ApprovalNotFound(approval_id)
            if approval.state == ApprovalState.EXPIRED.value:
                raise ApprovalExpired(approval_id)
            if approval.state == ApprovalState.REVOKED.value:
                raise ApprovalRevoked(approval_id)
            if approval.state == ApprovalState.CONSUMED.value:
                raise ApprovalAlreadyConsumed(approval_id)
            raise ApprovalAlreadyDecided(
                approval_id,
                state=approval.state,
                decision=approval.decision,
            )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        """Normalize SQLite round-tripped datetimes back to aware UTC.

        SQLite stores datetimes without a timezone, so repository reads return
        naive datetimes; attach UTC so comparisons never mix aware and naive
        values.
        """
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    @staticmethod
    def _require_actor(actor: str) -> None:
        if not actor or len(actor) > _MAX_ACTOR_LENGTH:
            raise ValueError("actor must be a non-empty identity label of at most 256 characters")
