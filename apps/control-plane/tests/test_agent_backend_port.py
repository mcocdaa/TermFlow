"""Contract tests for the backend-neutral Agent Backend port types (M0).

These tests freeze the executable port boundary between the Agent Broker
domain and any concrete backend adapter: neutral turn/notification/result
models, opaque conversation refs, an immutable capability matrix, and the
structural ``AgentBackend`` protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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
    StructuredPartFidelity,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FACTS,
    MAX_EVIDENCE_ITEMS,
    MAX_FACT_BYTES,
    AgentInboxEnvelope,
    BackendCancelRequest,
    BackendConversationRef,
    BackendConversationSnapshot,
    BackendEventScope,
    BackendInteraction,
    BackendInteractionKind,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnPart,
    BackendTurnRequest,
    ContextBlock,
    ContextFact,
    ContextFactTrust,
    CreateBackendConversation,
    EvidenceRecord,
    LeaseMetadata,
    NotificationPayload,
    TurnPartTrust,
)
from termflow_protocol.agent import (
    AgentActorKind,
    AgentEventKind,
    AgentInputDeliveryState,
    AgentInputKind,
    ApprovalDecision,
    BackendRuntimeState,
)


def _ref(**overrides: Any) -> BackendConversationRef:
    values: dict[str, Any] = {
        "backend_kind": "opencode",
        "backend_version": "0.1.0",
        "runtime_id": "runtime-1",
        "binding_capability_epoch": 3,
        "provider_ref": "provider-session-1",
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


def _capabilities() -> AgentBackendCapabilities:
    return AgentBackendCapabilities(
        streaming_output=True,
        context_mode=ContextMode.RESUME,
        submit_mode=SubmitMode.IDEMPOTENT,
        cancel_scope=CancelScope.RUN,
        replay_mode=ReplayMode.EVENT,
        concurrency_mode=ConcurrencyMode.SERIALIZED,
        tool_call_identity=ToolCallIdentity.RUN,
        runtime_isolation=RuntimeIsolation.BINDING,
        accepted_input_kinds=(AgentInputKind.USER_MESSAGE,),
        structured_part_fidelity=StructuredPartFidelity.FULL,
        tool_activity_events=True,
        permission_events=True,
        usage_cost_reporting=True,
        explicit_run_boundaries=True,
    )


class TestResultOutcomes:
    def test_submit_result_requires_explicit_outcome(self) -> None:
        result = BackendSubmitResult(outcome="retryable", retry_safe=True)
        assert result.outcome is BackendOutcome.RETRYABLE
        assert result.retry_safe is True

    def test_operation_result_requires_explicit_outcome(self) -> None:
        result = BackendOperationResult(outcome=BackendOutcome.UNSUPPORTED)
        assert result.outcome is BackendOutcome.UNSUPPORTED

    def test_conversation_snapshot_requires_explicit_outcome(self) -> None:
        snapshot = BackendConversationSnapshot(
            conversation_ref=_ref(),
            outcome=BackendOutcome.CONTEXT_LOST,
            resumable=False,
        )
        assert snapshot.outcome is BackendOutcome.CONTEXT_LOST

    @pytest.mark.parametrize(
        "builder",
        [
            lambda: BackendSubmitResult(outcome=None),  # type: ignore[arg-type]
            lambda: BackendOperationResult(outcome=None),  # type: ignore[arg-type]
            lambda: BackendConversationSnapshot(conversation_ref=_ref(), outcome=None),  # type: ignore[arg-type]
        ],
    )
    def test_none_outcome_is_rejected(self, builder: Any) -> None:
        with pytest.raises(ValidationError):
            builder()

    @pytest.mark.parametrize(
        "builder",
        [
            lambda: BackendSubmitResult(outcome="melted"),  # type: ignore[arg-type]
            lambda: BackendOperationResult(outcome="provider_lost_everything"),  # type: ignore[arg-type]
        ],
    )
    def test_invalid_outcome_strings_are_rejected(self, builder: Any) -> None:
        with pytest.raises(ValidationError):
            builder()

    def test_results_bind_evidence_and_retry_safety(self) -> None:
        evidence = (
            EvidenceRecord(key="http_status", value="429"),
            EvidenceRecord(key="provider_ref", value="provider-session-1"),
        )
        result = BackendSubmitResult(
            outcome=BackendOutcome.RETRYABLE,
            message="provider over capacity",
            evidence=evidence,
            retry_safe=True,
        )
        assert result.message == "provider over capacity"
        assert result.evidence == evidence
        assert result.retry_safe is True

    def test_results_reject_over_budget_evidence(self) -> None:
        too_much = tuple(
            EvidenceRecord(key=f"key-{index}", value="value")
            for index in range(MAX_EVIDENCE_ITEMS + 1)
        )
        with pytest.raises(ValidationError):
            BackendSubmitResult(outcome=BackendOutcome.CONFIRMED, evidence=too_much)
        with pytest.raises(ValidationError):
            BackendOperationResult(outcome=BackendOutcome.CONFIRMED, evidence=too_much)


class TestConversationRef:
    def test_provider_ref_round_trips_opaque(self) -> None:
        ref = _ref()
        assert ref.provider_ref == "provider-session-1"
        assert ref.backend_kind == "opencode"
        assert ref.backend_version == "0.1.0"
        assert ref.runtime_id == "runtime-1"
        assert ref.binding_capability_epoch == 3

    def test_ref_never_exposes_term_id_or_conversation_id(self) -> None:
        ref = _ref()
        assert not hasattr(ref, "term_id")
        assert not hasattr(ref, "conversation_id")
        assert "term_id" not in BackendConversationRef.model_fields
        assert "conversation_id" not in BackendConversationRef.model_fields

    def test_ref_rejects_term_id_field(self) -> None:
        with pytest.raises(ValidationError):
            _ref(term_id="provider-session-1")

    def test_ref_rejects_conversation_id_field(self) -> None:
        with pytest.raises(ValidationError):
            _ref(conversation_id="conv-1")


class TestNotification:
    def test_raw_provider_event_dict_is_rejected(self) -> None:
        raw_provider_event: dict[str, Any] = {
            "type": "message",
            "id": "msg_provider_1",
            "session_id": "sess_abc",
            "content": [{"type": "text", "text": "hello"}],
            "permissionID": "perm_1",
            "directory": "/tmp/workspace",
        }
        with pytest.raises(ValidationError):
            BackendNotification.model_validate(raw_provider_event)

    def test_notification_accepts_canonical_event_kind(self) -> None:
        notification = BackendNotification(
            conversation_ref=_ref(),
            scope=_scope(),
            kind="message_delta",
            run_id=uuid4(),
            message_id=uuid4(),
            dedup_key="dedup-1",
            payload=NotificationPayload(text="hi"),
        )
        assert notification.kind is AgentEventKind.MESSAGE_DELTA
        assert notification.scope.binding_id == "binding-1"
        assert notification.conversation_ref == _ref()

    def test_notification_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            BackendNotification(
                conversation_ref=_ref(),
                scope=_scope(),
                kind="provider_special_event",
                dedup_key="dedup-1",
                payload=NotificationPayload(text="x"),
            )

    def test_notification_payload_is_bounded_visible_content(self) -> None:
        notification = BackendNotification(
            conversation_ref=_ref(),
            scope=_scope(),
            kind=AgentEventKind.RUN_FAILED,
            run_id=uuid4(),
            dedup_key="dedup-2",
            payload=NotificationPayload(error_code="provider_error", error_message="boom"),
            observed_at=datetime.now(UTC),
        )
        assert notification.payload.error_code == "provider_error"
        assert notification.observed_at is not None


class TestAgentBackendCapabilities:
    def test_accepts_every_enum_mode(self) -> None:
        caps = _capabilities()
        assert caps.streaming_output is True
        assert caps.context_mode is ContextMode.RESUME
        assert caps.submit_mode is SubmitMode.IDEMPOTENT
        assert caps.cancel_scope is CancelScope.RUN
        assert caps.replay_mode is ReplayMode.EVENT
        assert caps.concurrency_mode is ConcurrencyMode.SERIALIZED
        assert caps.tool_call_identity is ToolCallIdentity.RUN
        assert caps.runtime_isolation is RuntimeIsolation.BINDING
        assert caps.accepted_input_kinds == (AgentInputKind.USER_MESSAGE,)
        assert caps.structured_part_fidelity is StructuredPartFidelity.FULL
        assert caps.tool_activity_events is True
        assert caps.permission_events is True
        assert caps.usage_cost_reporting is True
        assert caps.explicit_run_boundaries is True

    @pytest.mark.parametrize(
        "mode_field",
        [
            "context_mode",
            "submit_mode",
            "cancel_scope",
            "replay_mode",
            "concurrency_mode",
            "tool_call_identity",
            "runtime_isolation",
            "structured_part_fidelity",
        ],
    )
    def test_rejects_invalid_mode_strings(self, mode_field: str) -> None:
        invalid = _capabilities().model_dump()
        invalid[mode_field] = "not-a-mode"
        with pytest.raises(ValidationError):
            AgentBackendCapabilities(**invalid)

    def test_capabilities_are_frozen(self) -> None:
        caps = _capabilities()
        with pytest.raises(ValidationError):
            caps.streaming_output = False


class TestAgentBackendProtocol:
    def test_minimal_fake_satisfies_protocol(self) -> None:
        assert isinstance(_FakeBackend(), AgentBackend)

    def test_plain_object_does_not_satisfy_protocol(self) -> None:
        assert not isinstance(object(), AgentBackend)


class _FakeBackend:
    async def capabilities(self) -> AgentBackendCapabilities:
        return _capabilities()

    async def create_conversation(
        self, request: CreateBackendConversation
    ) -> BackendConversationRef:
        return _ref()

    async def submit(
        self, ref: BackendConversationRef, request: BackendTurnRequest
    ) -> BackendSubmitResult:
        return BackendSubmitResult(outcome=BackendOutcome.CONFIRMED, retry_safe=True)

    async def cancel(self, request: BackendCancelRequest) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)

    def events(self, scope: BackendEventScope): ...

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


class TestContextBlock:
    def test_facts_are_bounded_by_count(self) -> None:
        too_many = [
            ContextFact(text=f"fact-{index}", trust=ContextFactTrust.SYSTEM)
            for index in range(MAX_CONTEXT_FACTS + 1)
        ]
        with pytest.raises(ValidationError):
            ContextBlock(facts=too_many)

    def test_facts_are_bounded_by_total_bytes(self) -> None:
        max_allowed = [
            ContextFact(text="x" * MAX_FACT_BYTES, trust=ContextFactTrust.SYSTEM)
            for _ in range(MAX_CONTEXT_FACTS)
        ]
        assert sum(len(fact.text) for fact in max_allowed) > MAX_CONTEXT_BYTES
        with pytest.raises(ValidationError):
            ContextBlock(facts=max_allowed)

    def test_fact_is_bounded_per_item(self) -> None:
        with pytest.raises(ValidationError):
            ContextFact(
                text="x" * (MAX_FACT_BYTES + 1),
                trust=ContextFactTrust.SYSTEM,
            )

    def test_facts_carry_trust_and_disclosure_metadata(self) -> None:
        block = ContextBlock(
            facts=[
                ContextFact(text="user said no", trust=ContextFactTrust.USER_APPROVED),
                ContextFact(text="system injected fact", trust=ContextFactTrust.SYSTEM),
            ],
        )
        assert block.facts[0].trust is ContextFactTrust.USER_APPROVED
        assert block.facts[1].trust is ContextFactTrust.SYSTEM


class TestBackendTurnRequest:
    def test_carries_sanitized_metadata_only(self) -> None:
        request = _turn_request()
        assert request.conversation_ref == _ref()
        assert request.correlation_id == "corr-1"
        assert request.idempotency_key == "idem-1"
        assert request.parts[0].text == "please fix the build"
        assert request.parts[0].trust is TurnPartTrust.TRUSTED
        # Raw auth/lease state must never reach the backend.
        assert "auth" not in BackendTurnRequest.model_fields
        assert "lease" not in BackendTurnRequest.model_fields
        assert "actor_id" not in BackendTurnRequest.model_fields
        assert "admission_seq" not in BackendTurnRequest.model_fields

    def test_accepts_bounded_context_block(self) -> None:
        request = _turn_request().model_copy(
            update={
                "context": ContextBlock(
                    facts=[
                        ContextFact(
                            text="approved fact",
                            trust=ContextFactTrust.USER_APPROVED,
                        ),
                    ],
                ),
            }
        )
        assert request.context is not None
        assert request.context.facts[0].trust is ContextFactTrust.USER_APPROVED

    def test_parts_require_content(self) -> None:
        with pytest.raises(ValidationError):
            BackendTurnPart(kind=AgentInputKind.USER_MESSAGE, trust=TurnPartTrust.UNTRUSTED)


class TestCreateBackendConversation:
    def test_uses_opaque_workspace_alias_and_optional_parent(self) -> None:
        parent = _ref()
        request = CreateBackendConversation(
            display_title="Fix the build",
            workspace_alias="workspace-1",
            parent_ref=parent,
        )
        assert request.display_title == "Fix the build"
        assert request.workspace_alias == "workspace-1"
        assert request.parent_ref == parent


class TestBackendEventScope:
    def test_is_binding_and_runtime_scoped(self) -> None:
        conversation_id = uuid4()
        scope = BackendEventScope(
            binding_id="binding-1",
            runtime_epoch=4,
            conversation_id=conversation_id,
        )
        assert scope.binding_id == "binding-1"
        assert scope.runtime_epoch == 4
        assert scope.conversation_id == conversation_id

    def test_rejects_negative_epoch(self) -> None:
        with pytest.raises(ValidationError):
            BackendEventScope(binding_id="binding-1", runtime_epoch=-1)


class TestBackendInteraction:
    def test_permission_resolve_requires_permission_ref_and_decision(self) -> None:
        interaction = BackendInteraction(
            conversation_ref=_ref(),
            interaction_id=uuid4(),
            kind=BackendInteractionKind.PERMISSION_RESOLVE,
            permission_ref="provider-perm-1",
            decision=ApprovalDecision.APPROVED,
        )
        assert interaction.decision is ApprovalDecision.APPROVED
        with pytest.raises(ValidationError):
            BackendInteraction(
                conversation_ref=_ref(),
                interaction_id=uuid4(),
                kind=BackendInteractionKind.PERMISSION_RESOLVE,
            )


class TestAgentInboxEnvelope:
    def test_carries_admission_and_lease_metadata(self) -> None:
        envelope = AgentInboxEnvelope(
            conversation_id=uuid4(),
            actor_id="user-1",
            actor_kind=AgentActorKind.USER_SESSION,
            auth_epoch=2,
            admission_seq=7,
            delivery_state=AgentInputDeliveryState.PENDING,
            attempt=1,
            lease=LeaseMetadata(
                claim_token="claim-token-1",
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
            ),
        )
        assert envelope.actor_id == "user-1"
        assert envelope.actor_kind is AgentActorKind.USER_SESSION
        assert envelope.auth_epoch == 2
        assert envelope.admission_seq == 7
        assert envelope.delivery_state is AgentInputDeliveryState.PENDING
        assert envelope.attempt == 1
        assert envelope.lease.claim_token == "claim-token-1"

    def test_lease_expiry_must_be_after_claim(self) -> None:
        with pytest.raises(ValidationError):
            AgentInboxEnvelope(
                conversation_id=uuid4(),
                actor_id="user-1",
                actor_kind=AgentActorKind.USER_SESSION,
                admission_seq=1,
                lease=LeaseMetadata(
                    claim_token="claim-token-1",
                    expires_at=datetime.now(UTC) - timedelta(seconds=30),
                ),
            )


def test_snapshot_uses_canonical_runtime_state() -> None:
    snapshot = BackendConversationSnapshot(
        conversation_ref=_ref(),
        outcome=BackendOutcome.CONTEXT_LOST,
        state=BackendRuntimeState.CONTEXT_LOST,
    )
    assert snapshot.state is BackendRuntimeState.CONTEXT_LOST
