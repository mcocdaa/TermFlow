"""Agent run state machine tests (plan §7; task M1.4).

These tests exercise :class:`AgentRunStateMachine` and
:class:`SubmissionStateMachine` against a real migrated database
(``Database.initialize`` runs the packaged Alembic chain including migration
``0006``) wired through the same ``RepositoryBundle`` the agent repository
tests use.  A mutable ``Clock`` drives the state machine's time source so
``started_at``/``completed_at`` are deterministic.
"""

from __future__ import annotations

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
from termflow_control_plane.plugins.agent_broker.agent.runs import (
    AgentRunStateMachine,
    RunBoundary,
    RunStateError,
    SubmissionStateError,
    SubmissionStateMachine,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendEventScope,
    BackendNotification,
    BackendOutcome,
    NotificationPayload,
)
from termflow_protocol.agent import AgentEventKind


def _aware(value: datetime) -> datetime:
    """SQLite round-trips datetimes as naive; normalize for comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Clock:
    """Mutable time source so tests can drive run timestamps deterministically."""

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


def _machine(
    repos: RepositoryBundle,
    *,
    clock: Clock,
) -> AgentRunStateMachine:
    return AgentRunStateMachine(repos.agent_runs, now=clock)


def _caps(*, explicit_run_boundaries: bool = False) -> AgentBackendCapabilities:
    return AgentBackendCapabilities(
        context_mode=ContextMode.RESUME,
        submit_mode=SubmitMode.IDEMPOTENT,
        cancel_scope=CancelScope.RUN,
        replay_mode=ReplayMode.EVENT,
        concurrency_mode=ConcurrencyMode.SERIALIZED,
        tool_call_identity=ToolCallIdentity.BINDING,
        runtime_isolation=RuntimeIsolation.CONVERSATION,
        explicit_run_boundaries=explicit_run_boundaries,
    )


def _notification(kind: AgentEventKind) -> BackendNotification:
    return BackendNotification(
        conversation_ref=BackendConversationRef(
            backend_kind="opencode",
            backend_version="0.1.0",
            runtime_id="runtime://opencode",
            binding_capability_epoch=1,
            provider_ref="sess_abc",
        ),
        scope=BackendEventScope(
            binding_id="binding-1",
            runtime_epoch=1,
            conversation_id=uuid4(),
        ),
        kind=kind,
        dedup_key=f"dedup-{kind.value}-{uuid4().hex[:8]}",
        payload=NotificationPayload(),
    )


# ---------------------------------------------------------------------------
# Run transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_to_running_to_completed_happy_path(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    run_id = await machine.create_for_conversation(conversation_id)
    assert isinstance(run_id, UUID)
    queued = await repositories.agent_runs.get_by_id(run_id)
    assert queued is not None
    assert queued.run_state == "queued"
    assert _aware(queued.started_at) == observed

    started = await machine.start(run_id, backend_run_id="backend-run-1")
    assert started.run_state == "running"
    assert started.backend_run_id == "backend-run-1"

    completed = await machine.complete(run_id)
    assert completed.run_state == "completed"
    assert completed.completed_at == observed


@pytest.mark.asyncio
async def test_fail_records_error_code(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    run_id = await machine.create_for_conversation(conversation_id)
    await machine.start(run_id)
    failed = await machine.fail(run_id, error_code="backend_timeout")
    assert failed.run_state == "failed"
    assert failed.error_code == "backend_timeout"
    assert failed.completed_at == observed


@pytest.mark.asyncio
async def test_cancel_from_running(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    run_id = await machine.create_for_conversation(conversation_id)
    await machine.start(run_id)
    cancelled = await machine.cancel(run_id)
    assert cancelled.run_state == "cancelled"
    assert cancelled.completed_at == observed


@pytest.mark.asyncio
async def test_mark_unknown_from_running(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    run_id = await machine.create_for_conversation(conversation_id)
    await machine.start(run_id)
    unknown = await machine.mark_unknown(run_id)
    assert unknown.run_state == "unknown"
    assert unknown.completed_at == observed


@pytest.mark.asyncio
async def test_illegal_transitions_raise(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    run_id = await machine.create_for_conversation(conversation_id)

    # A queued run has not started, so no terminal transition may apply.
    with pytest.raises(RunStateError):
        await machine.complete(run_id)
    with pytest.raises(RunStateError):
        await machine.fail(run_id, error_code="nope")
    with pytest.raises(RunStateError):
        await machine.cancel(run_id)
    with pytest.raises(RunStateError):
        await machine.mark_unknown(run_id)

    # Starting twice is illegal.
    await machine.start(run_id)
    with pytest.raises(RunStateError):
        await machine.start(run_id)

    # Terminal states are final.
    await machine.complete(run_id)
    with pytest.raises(RunStateError):
        await machine.complete(run_id)
    with pytest.raises(RunStateError):
        await machine.cancel(run_id)
    with pytest.raises(RunStateError):
        await machine.fail(run_id, error_code="late")


@pytest.mark.asyncio
async def test_unknown_run_id_raises(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    machine = _machine(repositories, clock=clock)

    with pytest.raises(RunStateError):
        await machine.start(uuid4())
    with pytest.raises(RunStateError):
        await machine.complete(uuid4())


# ---------------------------------------------------------------------------
# create_for_conversation and one_active_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_for_conversation_and_one_active_run(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    clock = Clock(observed)
    conversation_id = await _seed_conversation(repositories)
    machine = _machine(repositories, clock=clock)

    first = await machine.create_for_conversation(conversation_id)
    second = await machine.create_for_conversation(conversation_id)
    assert first != second
    assert await machine.one_active_run(conversation_id) is False

    await machine.start(first)
    assert await machine.one_active_run(conversation_id) is True
    # A second queued run does not add to the active count.
    assert await machine.one_active_run(conversation_id) is True

    await machine.complete(first)
    assert await machine.one_active_run(conversation_id) is False

    # Runs are scoped per conversation.
    other_conversation = await _seed_conversation(repositories)
    assert await machine.one_active_run(other_conversation) is False


# ---------------------------------------------------------------------------
# infer_run_boundary (pure, deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AgentEventKind.RUN_STARTED, RunBoundary.START),
        (AgentEventKind.RUN_COMPLETED, RunBoundary.END),
        (AgentEventKind.RUN_FAILED, RunBoundary.END),
        (AgentEventKind.MESSAGE_COMPLETED, RunBoundary.NONE),
        (AgentEventKind.MESSAGE_DELTA, RunBoundary.NONE),
        (AgentEventKind.TOOL_STARTED, RunBoundary.NONE),
    ],
)
def test_infer_run_boundary_trusts_explicit_run_boundaries(
    kind: AgentEventKind,
    expected: RunBoundary,
) -> None:
    caps = _caps(explicit_run_boundaries=True)
    assert AgentRunStateMachine.infer_run_boundary(caps, _notification(kind)) is expected
    # StrEnum members compare equal to their string values.
    assert AgentRunStateMachine.infer_run_boundary(caps, _notification(kind)) == expected.value


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AgentEventKind.RUN_STARTED, RunBoundary.START),
        (AgentEventKind.RUN_COMPLETED, RunBoundary.END),
        (AgentEventKind.RUN_FAILED, RunBoundary.END),
        (AgentEventKind.MESSAGE_COMPLETED, RunBoundary.END),
        (AgentEventKind.MESSAGE_DELTA, RunBoundary.NONE),
        (AgentEventKind.TOOL_STARTED, RunBoundary.NONE),
        (AgentEventKind.TOOL_COMPLETED, RunBoundary.NONE),
        (AgentEventKind.PERMISSION_REQUESTED, RunBoundary.NONE),
        (AgentEventKind.BACKEND_STATE_CHANGED, RunBoundary.NONE),
    ],
)
def test_infer_run_boundary_infers_from_content_when_not_explicit(
    kind: AgentEventKind,
    expected: RunBoundary,
) -> None:
    caps = _caps(explicit_run_boundaries=False)
    assert AgentRunStateMachine.infer_run_boundary(caps, _notification(kind)) is expected


def test_infer_run_boundary_is_pure_and_deterministic() -> None:
    caps = _caps(explicit_run_boundaries=False)
    notification = _notification(AgentEventKind.MESSAGE_COMPLETED)
    first = AgentRunStateMachine.infer_run_boundary(caps, notification)
    second = AgentRunStateMachine.infer_run_boundary(caps, notification)
    assert first is second is RunBoundary.END


# ---------------------------------------------------------------------------
# map_submission_outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (BackendOutcome.CONFIRMED, "accepted"),
        (BackendOutcome.REQUESTED, "accepted"),
        (BackendOutcome.UNSUPPORTED, "rejected"),
        (BackendOutcome.RETRYABLE, "retryable"),
        (BackendOutcome.CONTEXT_LOST, "unknown"),
        (BackendOutcome.UNKNOWN, "unknown"),
    ],
)
def test_map_submission_outcome_covers_every_backend_outcome(
    outcome: BackendOutcome,
    expected: str,
) -> None:
    assert SubmissionStateMachine.map_submission_outcome(outcome) == expected
    # StrEnum values are accepted too, and the mapping is never None.
    mapped = SubmissionStateMachine.map_submission_outcome(outcome.value)
    assert mapped == expected
    assert mapped is not None


def test_map_submission_outcome_invalid_raises() -> None:
    with pytest.raises(SubmissionStateError):
        SubmissionStateMachine.map_submission_outcome("rejected")
    with pytest.raises(SubmissionStateError):
        SubmissionStateMachine.map_submission_outcome(None)  # type: ignore[arg-type]
    with pytest.raises(SubmissionStateError):
        SubmissionStateMachine.map_submission_outcome("bogus")
