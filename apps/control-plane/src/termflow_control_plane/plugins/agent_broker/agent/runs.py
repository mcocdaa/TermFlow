"""Agent run and submission state machines (plan §7; task M1.4).

Wraps :class:`~termflow_control_plane.persistence.repositories.AgentRunRepository`
with the plan §7 ``run_state`` state machine:

.. code-block:: text

    run_state:
    queued -> running -> completed|failed|cancelled|unknown

and maps backend submit outcomes (``BackendOutcome``) onto the plan §7
``submission_state`` vocabulary (``accepted``/``rejected``/``retryable``/
``unknown``).

Guard policy
============

The state machine uses a fail-loud policy, consistent with
:class:`~.inbox.InboxStateError`: an illegal transition (for example,
``complete`` on a run that has never been started) raises
:class:`RunStateError` instead of silently dropping the caller's intent.
The repository remains the last line of defence: every transition passes
``expected_state`` through ``set_state``, so a concurrent CAS rejection also
raises :class:`RunStateError`.

Run boundaries
==============

:meth:`AgentRunStateMachine.infer_run_boundary` is a pure, deterministic
decision of whether a normalized ``BackendNotification`` marks the start or
end of a run.  When the backend advertises ``explicit_run_boundaries`` B
trusts only the explicit ``run_started``/``run_completed``/``run_failed``
boundary events.  Otherwise B infers an end boundary from content events
(a ``message_completed`` proves the active run ended) while treating
streaming/tool/permission events as mid-run noise.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from termflow_protocol.agent import AgentEventKind

from termflow_control_plane.persistence.models import AgentRun
from termflow_control_plane.persistence.repositories import AgentRunRepository
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendNotification,
    BackendOutcome,
)

# Explicit backend-declared run boundary events.
_START_EVENT = AgentEventKind.RUN_STARTED
_END_EVENTS = (AgentEventKind.RUN_COMPLETED, AgentEventKind.RUN_FAILED)
# Content event that proves an active run ended when the backend does not
# declare explicit run boundaries.
_INFERRED_END_EVENT = AgentEventKind.MESSAGE_COMPLETED

# The only legal source state for each transition.
_SOURCE_STATE_FOR_TRANSITION = {
    "start": ("queued",),
    "complete": ("running",),
    "fail": ("running",),
    "cancel": ("running",),
    "mark_unknown": ("running",),
}


class RunStateError(Exception):
    """Raised when a run-state transition is illegal or rejected.

    The state machine uses a fail-loud policy: an illegal transition (or a
    transition the repository's CAS rejects) raises instead of silently
    dropping the caller's intent.
    """


class SubmissionStateError(Exception):
    """Raised when a backend outcome cannot be mapped to a submission state."""


class RunBoundary(StrEnum):
    """Whether a notification marks the start or end of a backend run."""

    START = "start"
    END = "end"
    NONE = "none"


class AgentRunStateMachine:
    """Lifecycle for one ``agent_runs`` row (plan §7 ``run_state``).

    ``now`` is a time-source callable (defaults to ``datetime.now(UTC)``) so
    ``started_at``/``completed_at`` are deterministic in tests.
    """

    def __init__(
        self,
        agent_runs: AgentRunRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._agent_runs = agent_runs
        self._clock = now or (lambda: datetime.now(UTC))

    async def create_for_conversation(self, conversation_id: UUID) -> UUID:
        """Create a run in ``queued`` for one conversation and return its id."""
        run = await self._agent_runs.create(
            conversation_id=conversation_id,
            run_state="queued",
            started_at=self._now(),
        )
        return run.id

    async def start(self, run_id: UUID, *, backend_run_id: str | None = None) -> AgentRun:
        """Transition ``queued -> running``, optionally recording a backend run id.

        The state transition is the durable gate; ``backend_run_id`` is
        attached afterwards so a concurrent CAS rejection cannot leave a
        backend id on a still-queued row.
        """
        current = await self._require(run_id, "start")
        self._guard(current, "start")
        started = await self._agent_runs.set_state(
            run_id,
            "running",
            expected_state="queued",
            now=self._now(),
        )
        if started is None:
            raise RunStateError(
                f"cannot start run {run_id}: "
                "repository rejected the queued -> running transition"
            )
        if backend_run_id is not None:
            updated = await self._agent_runs.set_backend_run_id(run_id, backend_run_id)
            if updated is None:
                raise RunStateError(f"cannot start run {run_id}: backend_run_id not recorded")
            return self._normalize(updated)
        return self._normalize(started)

    async def complete(self, run_id: UUID) -> AgentRun:
        """Transition ``running -> completed``."""
        return await self._terminal(run_id, "complete", "completed")

    async def fail(self, run_id: UUID, *, error_code: str) -> AgentRun:
        """Transition ``running -> failed``, persisting the error code."""
        return await self._terminal(run_id, "fail", "failed", error_code=error_code)

    async def cancel(self, run_id: UUID) -> AgentRun:
        """Transition ``running -> cancelled``."""
        return await self._terminal(run_id, "cancel", "cancelled")

    async def mark_unknown(self, run_id: UUID) -> AgentRun:
        """Transition ``running -> unknown`` (outcome unrecoverable)."""
        return await self._terminal(run_id, "mark_unknown", "unknown")

    async def one_active_run(self, conversation_id: UUID) -> bool:
        """Whether the conversation has a run currently in ``running``."""
        rows = await self._agent_runs.list_for_conversation(conversation_id)
        return any(row.run_state == "running" for row in rows)

    # ------------------------------------------------------------------
    # Pure policy helpers
    # ------------------------------------------------------------------

    @staticmethod
    def infer_run_boundary(
        caps: AgentBackendCapabilities,
        notification: BackendNotification,
    ) -> RunBoundary:
        """Classify a notification as a run ``start``/``end`` boundary.

        Pure and deterministic: no database access and no clock.  With
        ``explicit_run_boundaries`` B trusts the backend's declared boundary
        events; otherwise B additionally infers an end from
        ``message_completed`` (proof the active run finished) and treats
        streaming/tool/permission events as mid-run noise.
        """
        kind = notification.kind
        if kind is _START_EVENT:
            return RunBoundary.START
        if kind in _END_EVENTS:
            return RunBoundary.END
        if not caps.explicit_run_boundaries and kind is _INFERRED_END_EVENT:
            return RunBoundary.END
        return RunBoundary.NONE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require(self, run_id: UUID, verb: str) -> AgentRun:
        current = await self._agent_runs.get_by_id(run_id)
        if current is None:
            raise RunStateError(f"cannot {verb} run {run_id}: run does not exist")
        return current

    async def _terminal(
        self,
        run_id: UUID,
        verb: str,
        new_state: str,
        *,
        error_code: str | None = None,
    ) -> AgentRun:
        current = await self._require(run_id, verb)
        self._guard(current, verb)
        terminated = await self._agent_runs.set_state(
            run_id,
            new_state,
            expected_state="running",
            error_code=error_code,
            now=self._now(),
        )
        if terminated is None:
            raise RunStateError(
                f"cannot {verb} run {run_id}: "
                f"repository rejected the running -> {new_state} transition"
            )
        return self._normalize(terminated)

    def _guard(self, current: AgentRun, verb: str) -> None:
        allowed = _SOURCE_STATE_FOR_TRANSITION[verb]
        if current.run_state not in allowed:
            raise RunStateError(
                f"cannot {verb} run {current.id} from run_state={current.run_state!r}; "
                f"allowed: {', '.join(allowed)}"
            )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        """Normalize SQLite round-tripped datetimes back to aware UTC.

        SQLite stores datetimes without a timezone, so repository reads return
        naive datetimes; attach UTC so callers never mix aware and naive
        values.
        """
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def _normalize(self, run: AgentRun) -> AgentRun:
        """Return a run whose datetime columns are aware UTC datetimes."""
        run.started_at = self._aware(run.started_at)
        run.completed_at = self._aware(run.completed_at)
        return run

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class SubmissionStateMachine:
    """Maps backend submit outcomes onto the plan §7 submission vocabulary.

    The ``submission_state`` track is ``not_started -> started ->
    accepted|rejected|unknown`` with ``retryable`` as a transient outcome
    (see :mod:`.inbox`).  :meth:`map_submission_outcome` is pure and never
    returns ``None``; any outcome outside :class:`BackendOutcome` raises
    :class:`SubmissionStateError`.
    """

    _OUTCOME_TO_SUBMISSION_STATE = {
        BackendOutcome.CONFIRMED: "accepted",
        BackendOutcome.REQUESTED: "accepted",
        BackendOutcome.UNSUPPORTED: "rejected",
        BackendOutcome.RETRYABLE: "retryable",
        BackendOutcome.CONTEXT_LOST: "unknown",
        BackendOutcome.UNKNOWN: "unknown",
    }

    @classmethod
    def map_submission_outcome(cls, outcome: BackendOutcome | str) -> str:
        """Map a ``BackendOutcome`` to a durable submission state string.

        ``confirmed`` and ``requested`` are admissions (the backend took the
        request) and map to ``accepted``; ``unsupported`` is a definitive
        rejection and maps to ``rejected``; ``retryable`` stays ``retryable``;
        ``context_lost``/``unknown`` map to ``unknown``.  Anything else raises.
        """
        if isinstance(outcome, BackendOutcome):
            kind = outcome
        elif isinstance(outcome, str):
            try:
                kind = BackendOutcome(outcome)
            except ValueError:
                raise SubmissionStateError(
                    f"cannot map submission outcome: unknown backend outcome {outcome!r}"
                ) from None
        else:
            raise SubmissionStateError(
                "cannot map submission outcome: expected BackendOutcome, "
                f"got {type(outcome).__name__}"
            )
        return cls._OUTCOME_TO_SUBMISSION_STATE[kind]
