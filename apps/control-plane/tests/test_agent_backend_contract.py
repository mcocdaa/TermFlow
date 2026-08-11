"""Architecture contract tests for the Agent Backend port boundary (M0).

These tests prove the port is a real boundary, not prose:

- A structurally valid ``AgentBackend`` fake records the exact argument types
  it receives, and only neutral ``BackendTurnRequest``/``BackendCancelRequest``
  models ever cross it (never provider, persistence, or auth/B-internal state).
- ``BackendConversationRef`` keeps provider session IDs opaque: a raw provider
  ID can never be read as a product ``term_id``/``conversation_id``.
- ``BackendNotification`` only accepts normalized, provider-neutral events; a
  raw OpenCode-shaped event fails validation.
- Capability variants drive scheduler behavior through small pure functions
  that always return an explicit decision (never ``None``/void), and results
  always carry an explicit ``BackendOutcome``.

The scheduler decision functions below are deliberately pure and tiny; they
freeze the capability semantics the M1 scheduler must implement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackend,
    AgentBackendCapabilities,
    CancelScope,
    ConcurrencyMode,
    ContextMode,
    ReplayMode,
    RuntimeIsolation,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    AgentInboxEnvelope,
    BackendCancelRequest,
    BackendConversationRef,
    BackendConversationSnapshot,
    BackendEventScope,
    BackendInteraction,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnPart,
    BackendTurnRequest,
    CreateBackendConversation,
    LeaseMetadata,
    NotificationPayload,
    TurnPartTrust,
)
from termflow_protocol.agent import (
    AgentActorKind,
    AgentEventKind,
    AgentInputKind,
)

# ---------------------------------------------------------------------------
# Capability-driven scheduler decisions (pure, explicit, never None).
# ---------------------------------------------------------------------------


class SubmitAction(StrEnum):
    """What the scheduler does after a backend ``submit()`` result."""

    ACCEPTED = "accepted"
    AUTO_RETRY = "auto_retry"
    NEEDS_USER_RECOVERY = "needs_user_recovery"
    UNSUPPORTED = "unsupported"


class CancelAction(StrEnum):
    """What the scheduler may ask a backend to cancel."""

    CANCEL_RUN = "cancel_run"
    CANCEL_CONVERSATION = "cancel_conversation"
    UNSUPPORTED = "unsupported"


class TurnAdmission(StrEnum):
    """Whether a turn may be submitted right now."""

    ALLOWED = "allowed"
    BUSY = "busy"


class RestartAction(StrEnum):
    """How a conversation continues after a backend runtime restart."""

    RESUME = "resume"
    NEW_FORK = "new_fork"


class RestartDecision(NamedTuple):
    action: RestartAction
    context_preserved: bool


def decide_submit_retry(
    caps: AgentBackendCapabilities, result: BackendSubmitResult
) -> SubmitAction:
    """Decide how to proceed after a backend submit result.

    A ``retryable`` outcome may be retried automatically only when the backend
    guarantees idempotent admission (``submit_mode=idempotent``) and the result
    explicitly reports retry safety.  Every other ambiguous outcome requires
    explicit user recovery; it is never auto-retried.
    """
    if result.outcome is BackendOutcome.UNSUPPORTED:
        return SubmitAction.UNSUPPORTED
    if result.outcome is BackendOutcome.RETRYABLE:
        if caps.submit_mode is SubmitMode.IDEMPOTENT and result.retry_safe:
            return SubmitAction.AUTO_RETRY
        return SubmitAction.NEEDS_USER_RECOVERY
    if result.outcome in (BackendOutcome.CONFIRMED, BackendOutcome.REQUESTED):
        return SubmitAction.ACCEPTED
    return SubmitAction.NEEDS_USER_RECOVERY


def decide_cancel(
    caps: AgentBackendCapabilities, request: BackendCancelRequest
) -> CancelAction:
    """Decide whether a cancel request fits the backend's cancel scope.

    Fail-closed: a backend that cannot cancel (``cancel_scope=none``) or that
    cannot cancel the requested granularity returns an explicit
    ``UNSUPPORTED``, never a silent success.
    """
    if caps.cancel_scope is CancelScope.NONE:
        return CancelAction.UNSUPPORTED
    if request.run_id is not None:
        if caps.cancel_scope is CancelScope.RUN:
            return CancelAction.CANCEL_RUN
        return CancelAction.UNSUPPORTED
    if caps.cancel_scope is CancelScope.CONVERSATION:
        return CancelAction.CANCEL_CONVERSATION
    return CancelAction.UNSUPPORTED


def decide_submit_admission(
    caps: AgentBackendCapabilities, in_flight: int
) -> TurnAdmission:
    """Admit a submit only when the backend's concurrency mode allows it.

    A serialized backend keeps at most one in-flight turn; a parallel backend
    admits without a concurrency ceiling.
    """
    if caps.concurrency_mode is ConcurrencyMode.SERIALIZED and in_flight > 0:
        return TurnAdmission.BUSY
    return TurnAdmission.ALLOWED


def decide_restart(caps: AgentBackendCapabilities) -> RestartDecision:
    """Decide how a conversation continues after a backend runtime restart.

    ``resume`` backends keep runtime context and continue in place;
    ``fork_only`` backends need a new fork but preserve context; a
    ``lost_on_restart`` backend cannot continue and must be forked from B's
    curated transcript with no backend context carried over.
    """
    if caps.context_mode is ContextMode.RESUME:
        return RestartDecision(action=RestartAction.RESUME, context_preserved=True)
    if caps.context_mode is ContextMode.FORK_ONLY:
        return RestartDecision(action=RestartAction.NEW_FORK, context_preserved=True)
    return RestartDecision(action=RestartAction.NEW_FORK, context_preserved=False)


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _ref(**overrides: Any) -> BackendConversationRef:
    values: dict[str, Any] = {
        "backend_kind": "opencode",
        "backend_version": "0.1.0",
        "runtime_id": "runtime-1",
        "binding_capability_epoch": 3,
        "provider_ref": "opencode-sess-abc123",
    }
    values.update(overrides)
    return BackendConversationRef(**values)


def _scope() -> BackendEventScope:
    return BackendEventScope(binding_id="binding-1", runtime_epoch=1)


def _turn_request() -> BackendTurnRequest:
    return BackendTurnRequest(
        conversation_ref=_ref(),
        correlation_id="corr-1",
        idempotency_key="idem-1",
        parts=(
            BackendTurnPart(
                kind=AgentInputKind.USER_MESSAGE,
                text="please fix the build",
                trust=TurnPartTrust.TRUSTED,
            ),
        ),
    )


def _capabilities(**overrides: Any) -> AgentBackendCapabilities:
    values: dict[str, Any] = {
        "context_mode": ContextMode.RESUME,
        "submit_mode": SubmitMode.IDEMPOTENT,
        "cancel_scope": CancelScope.RUN,
        "replay_mode": ReplayMode.EVENT,
        "concurrency_mode": ConcurrencyMode.SERIALIZED,
        "tool_call_identity": ToolCallIdentity.RUN,
        "runtime_isolation": RuntimeIsolation.BINDING,
    }
    values.update(overrides)
    return AgentBackendCapabilities(**values)


def _run_cancel_request() -> BackendCancelRequest:
    return BackendCancelRequest(
        conversation_ref=_ref(),
        run_id=uuid4(),
        correlation_id="corr-cancel-run",
        reason="test",
    )


def _conversation_cancel_request() -> BackendCancelRequest:
    return BackendCancelRequest(
        conversation_ref=_ref(),
        correlation_id="corr-cancel-conversation",
        reason="test",
    )


def _provider_shaped_payload() -> dict[str, object]:
    """An OpenCode-style transport payload that must never reach the port."""
    return {
        "sessionId": "opencode-sess-abc123",
        "directory": "/tmp/workspace",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "please fix the build"}],
            }
        ],
        "model": "gpt-5",
    }


def _auth_shaped_payload() -> dict[str, object]:
    """B-internal auth/lease state that must never cross the boundary."""
    return {
        "actor_id": "user-1",
        "actor_kind": "user_session",
        "auth_epoch": 2,
        "admission_seq": 7,
        "lease": {
            "claim_token": "claim-token-1",
            "expires_at": "2026-08-11T10:00:00Z",
        },
    }


def _persistence_shaped_payload() -> dict[str, object]:
    """A durable B inbox-envelope row that must never leak to the adapter."""
    envelope = AgentInboxEnvelope(
        conversation_id=uuid4(),
        actor_id="user-1",
        actor_kind=AgentActorKind.USER_SESSION,
        auth_epoch=2,
        admission_seq=7,
        lease=LeaseMetadata(
            claim_token="claim-token-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        ),
    )
    return envelope.model_dump()


def _raw_shapes() -> list[dict[str, object]]:
    return [
        _provider_shaped_payload(),
        _auth_shaped_payload(),
        _persistence_shaped_payload(),
    ]


def deliver(request: BackendTurnRequest) -> BackendTurnRequest:
    """The only shape the adapter is allowed to receive at the port.

    A typed function like this is the compile-time half of the boundary;
    provider/persistence/auth-shaped objects are neither instances of
    ``BackendTurnRequest`` nor accepted by its validator.
    """
    return request


# ---------------------------------------------------------------------------
# Fake backend: structurally valid AgentBackend that records port traffic.
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal backend adapter recording the argument types it receives.

    Every operation returns an explicit :class:`BackendOutcome`; nothing ever
    returns ``None`` where the port requires a result.
    """

    def __init__(self, capabilities: AgentBackendCapabilities | None = None) -> None:
        self._capabilities = capabilities or _capabilities()
        self.submitted_ref_types: list[type] = []
        self.submitted_request_types: list[type] = []
        self.submit_results: list[BackendSubmitResult] = []
        self.cancel_request_types: list[type] = []
        self.cancel_results: list[BackendOperationResult] = []

    async def capabilities(self) -> AgentBackendCapabilities:
        return self._capabilities

    async def create_conversation(
        self, request: CreateBackendConversation
    ) -> BackendConversationRef:
        return _ref()

    async def submit(
        self, ref: BackendConversationRef, request: BackendTurnRequest
    ) -> BackendSubmitResult:
        self.submitted_ref_types.append(type(ref))
        self.submitted_request_types.append(type(request))
        result = BackendSubmitResult(outcome=BackendOutcome.CONFIRMED, retry_safe=True)
        self.submit_results.append(result)
        return result

    async def cancel(self, request: BackendCancelRequest) -> BackendOperationResult:
        self.cancel_request_types.append(type(request))
        result = BackendOperationResult(outcome=BackendOutcome.CONFIRMED)
        self.cancel_results.append(result)
        return result

    async def events(self, scope: BackendEventScope) -> AsyncIterator[BackendNotification]:
        if False:
            yield BackendNotification(
                conversation_ref=_ref(),
                scope=_scope(),
                kind=AgentEventKind.RUN_STARTED,
                dedup_key="never",
                payload=NotificationPayload(),
            )

    async def reconcile(self, ref: BackendConversationRef) -> BackendConversationSnapshot:
        return BackendConversationSnapshot(
            conversation_ref=ref,
            outcome=BackendOutcome.CONFIRMED,
        )

    async def interact(self, request: BackendInteraction) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)

    async def delete_conversation(self, ref: BackendConversationRef) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)

    async def close(self) -> None: ...


@pytest.fixture
def fake_backend() -> FakeBackend:
    """A structurally valid ``AgentBackend`` that records port traffic."""
    return FakeBackend()


# ---------------------------------------------------------------------------
# Port boundary: only neutral models cross it.
# ---------------------------------------------------------------------------


class TestAgentBackendProtocol:
    def test_fake_backend_satisfies_the_structural_protocol(
        self, fake_backend: FakeBackend
    ) -> None:
        assert isinstance(fake_backend, AgentBackend)

    def test_plain_object_does_not_satisfy_the_protocol(self) -> None:
        assert not isinstance(object(), AgentBackend)


class TestPortBoundary:
    def test_raw_shapes_are_never_backend_turn_requests(self) -> None:
        for raw in _raw_shapes():
            assert not isinstance(raw, BackendTurnRequest)

    def test_typed_delivery_rejects_raw_shapes_at_validation(self) -> None:
        # The only thing that type-checks into ``deliver`` is a neutral turn.
        assert isinstance(deliver(_turn_request()), BackendTurnRequest)
        for raw in _raw_shapes():
            with pytest.raises(ValidationError):
                BackendTurnRequest.model_validate(raw)

    async def test_submit_port_receives_only_neutral_turn_requests(
        self, fake_backend: FakeBackend
    ) -> None:
        async def broker_deliver() -> BackendSubmitResult:
            # The broker's submit path may only hand neutral models across.
            return await fake_backend.submit(_ref(), deliver(_turn_request()))

        result = await broker_deliver()
        assert result.outcome is BackendOutcome.CONFIRMED
        assert fake_backend.submitted_ref_types == [BackendConversationRef]
        assert fake_backend.submitted_request_types == [BackendTurnRequest]
        assert all(item.outcome is BackendOutcome.CONFIRMED for item in fake_backend.submit_results)

    async def test_cancel_port_receives_only_neutral_requests(
        self, fake_backend: FakeBackend
    ) -> None:
        await fake_backend.cancel(_run_cancel_request())
        assert fake_backend.cancel_request_types == [BackendCancelRequest]
        assert all(item.outcome is BackendOutcome.CONFIRMED for item in fake_backend.cancel_results)

    async def test_capabilities_are_explicit_and_drive_the_scheduler(
        self, fake_backend: FakeBackend
    ) -> None:
        caps = await fake_backend.capabilities()
        assert caps.submit_mode is SubmitMode.IDEMPOTENT
        assert caps.cancel_scope is CancelScope.RUN
        assert caps.context_mode is ContextMode.RESUME
        assert caps.concurrency_mode is ConcurrencyMode.SERIALIZED


# ---------------------------------------------------------------------------
# Provider ID opacity.
# ---------------------------------------------------------------------------


class TestProviderIdOpacity:
    def test_ref_wraps_provider_ref_opaque_and_never_a_product_id(self) -> None:
        provider_session_id = "opencode-sess-abc123"
        ref = _ref(provider_ref=provider_session_id)
        assert ref.provider_ref == provider_session_id
        assert "term_id" not in BackendConversationRef.model_fields
        assert "conversation_id" not in BackendConversationRef.model_fields
        assert not hasattr(ref, "term_id")
        assert not hasattr(ref, "conversation_id")

    def test_provider_session_id_cannot_be_read_as_a_product_id(self) -> None:
        provider_session_id = "opencode-sess-abc123"
        # Product conversation ids are UUIDs; a raw provider session id is not.
        with pytest.raises(ValueError):
            UUID(provider_session_id)
        # The ref carries its own opaque provider string, not a product id.
        ref = _ref(provider_ref=provider_session_id)
        assert isinstance(ref.provider_ref, str)


# ---------------------------------------------------------------------------
# Notification normalization.
# ---------------------------------------------------------------------------


class TestNotificationNormalization:
    def test_raw_opencode_event_dict_is_rejected(self) -> None:
        raw_opencode_event: dict[str, object] = {
            "type": "message",
            "id": "msg_provider_1",
            "sessionId": "opencode-sess-abc123",
            "part": {"type": "text", "text": "hello"},
            "permissionID": "perm_1",
            "directory": "/tmp/workspace",
            "timestamp": "2026-08-11T10:00:00Z",
        }
        with pytest.raises(ValidationError):
            BackendNotification.model_validate(raw_opencode_event)

    def test_normalized_notification_validates(self) -> None:
        notification = BackendNotification(
            conversation_ref=_ref(),
            scope=_scope(),
            kind=AgentEventKind.MESSAGE_DELTA,
            run_id=uuid4(),
            message_id=uuid4(),
            dedup_key="dedup-1",
            payload=NotificationPayload(text="hello"),
        )
        assert notification.kind is AgentEventKind.MESSAGE_DELTA
        assert notification.scope.binding_id == "binding-1"
        assert notification.conversation_ref.provider_ref == "opencode-sess-abc123"
        assert notification.dedup_key == "dedup-1"


# ---------------------------------------------------------------------------
# Capability-driven scheduler behavior.
# ---------------------------------------------------------------------------


class TestSubmitRetryPolicy:
    def test_idempotent_backend_auto_retries_retryable_outcome(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.IDEMPOTENT)
        result = BackendSubmitResult(outcome=BackendOutcome.RETRYABLE, retry_safe=True)
        assert decide_submit_retry(caps, result) is SubmitAction.AUTO_RETRY

    def test_non_idempotent_backend_never_auto_retries(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.NON_IDEMPOTENT)
        result = BackendSubmitResult(outcome=BackendOutcome.RETRYABLE, retry_safe=True)
        assert decide_submit_retry(caps, result) is SubmitAction.NEEDS_USER_RECOVERY

    def test_unknown_submit_mode_never_auto_retries(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.UNKNOWN)
        result = BackendSubmitResult(outcome=BackendOutcome.RETRYABLE, retry_safe=True)
        assert decide_submit_retry(caps, result) is SubmitAction.NEEDS_USER_RECOVERY

    def test_retryable_outcome_without_retry_safety_needs_user_recovery(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.IDEMPOTENT)
        result = BackendSubmitResult(outcome=BackendOutcome.RETRYABLE, retry_safe=False)
        assert decide_submit_retry(caps, result) is SubmitAction.NEEDS_USER_RECOVERY

    def test_unsupported_outcome_is_explicit(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.NON_IDEMPOTENT)
        result = BackendSubmitResult(outcome=BackendOutcome.UNSUPPORTED)
        assert decide_submit_retry(caps, result) is SubmitAction.UNSUPPORTED

    def test_confirmed_outcome_is_accepted(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.NON_IDEMPOTENT)
        result = BackendSubmitResult(outcome=BackendOutcome.CONFIRMED, retry_safe=True)
        assert decide_submit_retry(caps, result) is SubmitAction.ACCEPTED

    def test_every_outcome_yields_an_explicit_decision(self) -> None:
        caps = _capabilities(submit_mode=SubmitMode.NON_IDEMPOTENT)
        for outcome in BackendOutcome:
            action = decide_submit_retry(
                caps, BackendSubmitResult(outcome=outcome, retry_safe=True)
            )
            assert action is not None
            assert isinstance(action, SubmitAction)


class TestCancelPolicy:
    def test_cancel_scope_none_fails_closed(self) -> None:
        caps = _capabilities(cancel_scope=CancelScope.NONE)
        action = decide_cancel(caps, _run_cancel_request())
        assert action is CancelAction.UNSUPPORTED
        assert action is not None

    def test_run_scope_allows_run_scoped_cancel(self) -> None:
        caps = _capabilities(cancel_scope=CancelScope.RUN)
        assert decide_cancel(caps, _run_cancel_request()) is CancelAction.CANCEL_RUN

    def test_run_scope_rejects_conversation_scoped_cancel(self) -> None:
        caps = _capabilities(cancel_scope=CancelScope.RUN)
        action = decide_cancel(caps, _conversation_cancel_request())
        assert action is CancelAction.UNSUPPORTED
        assert action is not None

    def test_conversation_scope_allows_conversation_scoped_cancel(self) -> None:
        caps = _capabilities(cancel_scope=CancelScope.CONVERSATION)
        assert (
            decide_cancel(caps, _conversation_cancel_request())
            is CancelAction.CANCEL_CONVERSATION
        )

    def test_conversation_scope_rejects_run_scoped_cancel(self) -> None:
        caps = _capabilities(cancel_scope=CancelScope.CONVERSATION)
        action = decide_cancel(caps, _run_cancel_request())
        assert action is CancelAction.UNSUPPORTED
        assert action is not None


class TestRestartContinuation:
    def test_resume_backend_continues_in_place(self) -> None:
        caps = _capabilities(context_mode=ContextMode.RESUME)
        decision = decide_restart(caps)
        assert decision.action is RestartAction.RESUME
        assert decision.context_preserved is True

    def test_fork_only_backend_requires_a_new_fork_but_keeps_context(self) -> None:
        caps = _capabilities(context_mode=ContextMode.FORK_ONLY)
        decision = decide_restart(caps)
        assert decision.action is RestartAction.NEW_FORK
        assert decision.context_preserved is True

    def test_lost_on_restart_backend_cannot_continue(self) -> None:
        caps = _capabilities(context_mode=ContextMode.LOST_ON_RESTART)
        decision = decide_restart(caps)
        assert decision.action is RestartAction.NEW_FORK
        assert decision.context_preserved is False


class TestTurnAdmission:
    def test_serialized_backend_keeps_at_most_one_in_flight_turn(self) -> None:
        caps = _capabilities(concurrency_mode=ConcurrencyMode.SERIALIZED)
        assert decide_submit_admission(caps, in_flight=0) is TurnAdmission.ALLOWED
        assert decide_submit_admission(caps, in_flight=1) is TurnAdmission.BUSY
        assert decide_submit_admission(caps, in_flight=2) is TurnAdmission.BUSY

    def test_parallel_backend_allows_concurrent_turns(self) -> None:
        caps = _capabilities(concurrency_mode=ConcurrencyMode.PARALLEL)
        assert decide_submit_admission(caps, in_flight=0) is TurnAdmission.ALLOWED
        assert decide_submit_admission(caps, in_flight=1) is TurnAdmission.ALLOWED
        assert decide_submit_admission(caps, in_flight=4) is TurnAdmission.ALLOWED


class TestExplicitOutcomes:
    def test_submit_result_requires_an_explicit_outcome(self) -> None:
        with pytest.raises(ValidationError):
            BackendSubmitResult()
        with pytest.raises(ValidationError):
            BackendSubmitResult(outcome=None)  # type: ignore[arg-type]

    def test_operation_result_requires_an_explicit_outcome(self) -> None:
        with pytest.raises(ValidationError):
            BackendOperationResult()
        with pytest.raises(ValidationError):
            BackendOperationResult(outcome=None)  # type: ignore[arg-type]

    def test_valid_results_carry_a_backend_outcome_member(self) -> None:
        submit = BackendSubmitResult(outcome=BackendOutcome.CONFIRMED)
        operation = BackendOperationResult(outcome=BackendOutcome.UNSUPPORTED)
        assert submit.outcome is BackendOutcome.CONFIRMED
        assert operation.outcome is BackendOutcome.UNSUPPORTED
