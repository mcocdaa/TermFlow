"""Backend-neutral turn, notification, and result models for the Agent Broker.

These models form the port boundary between the Agent Broker domain and any
concrete backend adapter.  Backend-specific session IDs, provider event
shapes, raw provider payloads, and B-internal auth/lease state never leak
across this boundary.

The canonical input/event kinds come from ``termflow_protocol.agent``; the
adapter normalizes provider events into these neutral shapes and B never sees
provider-private data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import NewType
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from termflow_protocol.agent import (
    AgentActorKind,
    AgentEventKind,
    AgentInputDeliveryState,
    AgentInputKind,
    ApprovalDecision,
    BackendRuntimeState,
)
from termflow_protocol.common import utc_now

KIB = 1024

MAX_CONTEXT_FACTS = 64
MAX_FACT_BYTES = 4 * KIB
MAX_CONTEXT_BYTES = 128 * KIB
MAX_EVIDENCE_ITEMS = 32
MAX_PART_TEXT_BYTES = 16 * KIB
MAX_NOTIFICATION_TEXT_BYTES = 8 * KIB
MAX_REF_LENGTH = 2048


class BackendModel(BaseModel):
    """Strict base so port drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


class ContextFactTrust(StrEnum):
    """Trust label attached to a projected memory fact."""

    USER_APPROVED = "user_approved"
    SUGGESTION = "suggestion"
    SYSTEM = "system"


class ContextFact(BackendModel):
    """One size-bounded, trust-labelled memory fact from the projection."""

    text: str = Field(min_length=1, max_length=MAX_FACT_BYTES)
    trust: ContextFactTrust
    source_ref: str | None = Field(default=None, min_length=1, max_length=MAX_REF_LENGTH)
    revision: int = Field(default=0, ge=0)
    expires_at: datetime | None = None


class DisclosureMetadata(BackendModel):
    """Disclosure policy metadata attached to content leaving B.

    ``display_title`` and projected facts are redacted according to this
    disclosure before the request is handed to the backend.
    """

    policy_version: str = Field(min_length=1, max_length=128)
    no_training: bool = False
    retention_terms: str | None = Field(default=None, min_length=1, max_length=512)
    disclosed_at: datetime = Field(default_factory=utc_now)


class ContextBlock(BackendModel):
    """Bounded memory projection passed to a backend for one turn.

    B's assembler selects approved/system facts, adds trust labels and
    disclosure metadata, and emits this size-bounded block.  Backend adapters
    can never reconstruct or widen the memory scope themselves.
    """

    facts: list[ContextFact] = Field(default_factory=list)
    disclosure: DisclosureMetadata | None = None

    @model_validator(mode="after")
    def context_is_within_budget(self) -> ContextBlock:
        if len(self.facts) > MAX_CONTEXT_FACTS:
            raise ValueError(f"context facts must not exceed {MAX_CONTEXT_FACTS}")
        total_bytes = sum(len(fact.text.encode("utf-8")) for fact in self.facts)
        if total_bytes > MAX_CONTEXT_BYTES:
            raise ValueError(f"context facts must not exceed {MAX_CONTEXT_BYTES} UTF-8 bytes")
        return self


class TurnPartTrust(StrEnum):
    """Trust label for typed content carried inside a turn request."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class BackendTurnPart(BackendModel):
    """One normalized, typed content part with its trust label."""

    kind: AgentInputKind
    text: str | None = Field(default=None, max_length=MAX_PART_TEXT_BYTES)
    structured_ref: str | None = Field(default=None, min_length=1, max_length=MAX_REF_LENGTH)
    trust: TurnPartTrust = TurnPartTrust.UNTRUSTED

    @model_validator(mode="after")
    def part_carries_content(self) -> BackendTurnPart:
        if self.text is None and self.structured_ref is None:
            raise ValueError("turn parts require text or a structured reference")
        return self


class TurnSizePolicy(BackendModel):
    """Size budget the backend must respect for one turn."""

    max_input_bytes: int = Field(default=MAX_PART_TEXT_BYTES, ge=1)
    max_output_bytes: int = Field(default=MAX_CONTEXT_BYTES, ge=1)
    max_context_bytes: int = Field(default=MAX_CONTEXT_BYTES, ge=1)


class TurnPolicy(BackendModel):
    """Policy metadata bounding backend behaviour for one turn."""

    tool_use: bool = True
    permission_resolution: bool = True
    ephemeral: bool = False


class BackendOutcome(StrEnum):
    """Explicit provider-operation outcome; never ``None``."""

    CONFIRMED = "confirmed"
    REQUESTED = "requested"
    UNSUPPORTED = "unsupported"
    RETRYABLE = "retryable"
    CONTEXT_LOST = "context_lost"
    UNKNOWN = "unknown"


class EvidenceRecord(BackendModel):
    """One bounded evidence record attached to a provider result."""

    key: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=2048)
    detail: str = Field(default="", max_length=4096)


def _validate_evidence_bounded(
    value: tuple[EvidenceRecord, ...],
) -> tuple[EvidenceRecord, ...]:
    if len(value) > MAX_EVIDENCE_ITEMS:
        raise ValueError(f"evidence must not exceed {MAX_EVIDENCE_ITEMS} records")
    return value


class BackendSubmitResult(BackendModel):
    """Admission/transport acceptance for one backend submit.

    ``submit()`` reports only acceptance; run completion arrives through
    normalized events or reconciliation.
    """

    outcome: BackendOutcome
    message: str = Field(default="", max_length=4096)
    evidence: tuple[EvidenceRecord, ...] = Field(default_factory=tuple)
    retry_safe: bool = False

    @field_validator("evidence")
    @classmethod
    def evidence_is_bounded(
        cls, value: tuple[EvidenceRecord, ...]
    ) -> tuple[EvidenceRecord, ...]:
        return _validate_evidence_bounded(value)


class BackendOperationResult(BackendModel):
    """Outcome for cancel, interact, and delete-conversation operations."""

    outcome: BackendOutcome
    message: str = Field(default="", max_length=4096)
    evidence: tuple[EvidenceRecord, ...] = Field(default_factory=tuple)
    retry_safe: bool = False

    @field_validator("evidence")
    @classmethod
    def evidence_is_bounded(
        cls, value: tuple[EvidenceRecord, ...]
    ) -> tuple[EvidenceRecord, ...]:
        return _validate_evidence_bounded(value)


class BackendCancelRequest(BackendModel):
    """Request to cancel a run or the whole conversation on a backend."""

    conversation_ref: BackendConversationRef
    run_id: UUID | None = None
    correlation_id: str = Field(min_length=1, max_length=256)
    reason: str | None = Field(default=None, max_length=512)


class BackendInteractionKind(StrEnum):
    """Normalized interaction kinds a backend may expose."""

    PERMISSION_RESOLVE = "permission_resolve"


class BackendInteraction(BackendModel):
    """B resolution of a pending backend interaction (for example, a permission)."""

    conversation_ref: BackendConversationRef
    interaction_id: UUID = Field(default_factory=uuid4)
    kind: BackendInteractionKind
    permission_ref: str | None = Field(default=None, min_length=1, max_length=MAX_REF_LENGTH)
    decision: ApprovalDecision | None = None

    @model_validator(mode="after")
    def permission_resolve_is_complete(self) -> BackendInteraction:
        if self.kind is BackendInteractionKind.PERMISSION_RESOLVE:
            if self.permission_ref is None or self.decision is None:
                raise ValueError("permission_resolve requires a permission_ref and decision")
        return self


class BackendConversationSnapshot(BackendModel):
    """Reconciliation snapshot proving message/run state after a disconnect."""

    conversation_ref: BackendConversationRef
    outcome: BackendOutcome
    state: BackendRuntimeState = BackendRuntimeState.RECONCILING
    resumable: bool = False
    message_count: int | None = Field(default=None, ge=0)
    last_observed_at: datetime | None = None
    evidence: tuple[EvidenceRecord, ...] = Field(default_factory=tuple)

    @field_validator("evidence")
    @classmethod
    def evidence_is_bounded(
        cls, value: tuple[EvidenceRecord, ...]
    ) -> tuple[EvidenceRecord, ...]:
        return _validate_evidence_bounded(value)


class BackendEventScope(BackendModel):
    """Binding/runtime scope a backend event subscription is created for."""

    binding_id: str = Field(min_length=1, max_length=256)
    runtime_epoch: int = Field(ge=0)
    conversation_id: UUID | None = None


BackendWorkspaceAlias = NewType("BackendWorkspaceAlias", str)


class CreateBackendConversation(BackendModel):
    """Neutral create-conversation request.

    Provider model IDs, agent names, and routing fields stay in adapter
    configuration and opaque refs; ``display_title`` is redacted according to
    the disclosure policy before leaving B.
    """

    display_title: str = Field(min_length=1, max_length=256)
    workspace_alias: BackendWorkspaceAlias = Field(min_length=1, max_length=MAX_REF_LENGTH)
    parent_ref: BackendConversationRef | None = None


ProviderRef = NewType("ProviderRef", str)


class BackendConversationRef(BackendModel):
    """Opaque, backend-scoped conversation reference.

    A bare provider session ID is never sufficient to resume or authorize a
    call, so the ref also records backend kind/version, runtime identity, and
    the binding capability epoch.  The provider ref is opaque: B must never
    interpret its contents.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_kind: str = Field(min_length=1, max_length=128)
    backend_version: str = Field(min_length=1, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=256)
    binding_capability_epoch: int = Field(ge=0)
    provider_ref: ProviderRef = Field(min_length=1, max_length=MAX_REF_LENGTH)


class NotificationPayload(BackendModel):
    """Bounded, visible payload carried by a normalized notification.

    Reasoning, attachments, provider-private metadata, raw tool results, and
    unknown payloads are dropped by the adapter and never appear here.
    """

    text: str | None = Field(default=None, max_length=MAX_NOTIFICATION_TEXT_BYTES)
    summary: str | None = Field(default=None, max_length=MAX_NOTIFICATION_TEXT_BYTES)
    error_code: str | None = Field(default=None, min_length=1, max_length=256)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)


class BackendNotification(BackendModel):
    """Normalized, provider-neutral event returned by a scoped subscription.

    Contains only opaque backend refs, binding/runtime scope, a normalized
    ``AgentEventKind``, optional run/message/part/tool IDs, a stable dedup
    key, a bounded visible payload, and ``observed_at``.
    """

    notification_id: UUID = Field(default_factory=uuid4)
    conversation_ref: BackendConversationRef
    scope: BackendEventScope
    kind: AgentEventKind
    run_id: UUID | None = None
    message_id: UUID | None = None
    part_id: str | None = Field(default=None, min_length=1, max_length=256)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    dedup_key: str = Field(min_length=1, max_length=256)
    payload: NotificationPayload
    observed_at: datetime = Field(default_factory=utc_now)


class LeaseMetadata(BackendModel):
    """Lease/fencing metadata attached to a B-only inbox envelope."""

    lease_id: UUID = Field(default_factory=uuid4)
    claim_token: str = Field(min_length=1, max_length=256)
    claimed_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    attempt_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def expiry_after_claim(self) -> LeaseMetadata:
        if self.expires_at <= self.claimed_at:
            raise ValueError("lease expiry must be after claim time")
        return self


class AgentInboxEnvelope(BackendModel):
    """B-only envelope describing one inbox item to the Agent domain.

    Carries actor identity, auth epoch, admission sequence, delivery state,
    attempt count, and lease metadata.  This envelope never crosses the
    backend boundary; the adapter receives only a sanitized
    :class:`BackendTurnRequest`.
    """

    envelope_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    actor_id: str = Field(min_length=1, max_length=256)
    actor_kind: AgentActorKind
    auth_epoch: int | None = Field(default=None, ge=0)
    admission_seq: int = Field(ge=1)
    delivery_state: AgentInputDeliveryState = AgentInputDeliveryState.PENDING
    attempt: int = Field(default=0, ge=0)
    lease: LeaseMetadata


class BackendTurnRequest(BackendModel):
    """Sanitized turn request handed to a backend adapter.

    Contains opaque conversation/backend refs, B correlation and idempotency
    keys, typed content parts with trust labels, a bounded
    :class:`ContextBlock`, and disclosure/size/policy metadata.  It
    explicitly never carries raw auth or lease state.
    """

    conversation_ref: BackendConversationRef
    correlation_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    parts: tuple[BackendTurnPart, ...]
    context: ContextBlock | None = None
    disclosure: DisclosureMetadata | None = None
    size_policy: TurnSizePolicy = Field(default_factory=TurnSizePolicy)
    turn_policy: TurnPolicy = Field(default_factory=TurnPolicy)
