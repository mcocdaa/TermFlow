"""Canonical Agent Input and Event wire models for the Agent Broker.

These are the neutral, provider-agnostic contracts that flow into the Agent
Inbox (``AgentInput``) and out of the Agent Backend adapter into B and C
(``AgentEvent``). Backend-specific session IDs, event shapes, and provider
parts never leak into these models.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .common import utc_now
from .messages import PayloadModel, validate_plain_text

MAX_AGENT_TEXT_BYTES = 64 * 1024
MAX_REF_LENGTH = 2048


def validate_agent_text(text: str, *, max_bytes: int = MAX_AGENT_TEXT_BYTES) -> str:
    """Validate agent chat text while keeping intentional line breaks.

    The Web composer strips every control character except newline, so the
    server mirrors that contract: a multi-line prompt is accepted, while
    terminal pane input keeps the stricter single-line ``validate_plain_text``.
    """

    if any(
        (ord(character) < 32 and character != "\n") or 127 <= ord(character) <= 159
        for character in text
    ):
        raise ValueError("text contains unsupported control characters")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"text exceeds {max_bytes} UTF-8 bytes")
    return text


class AgentInputKind(StrEnum):
    USER_MESSAGE = "user_message"
    WATCH_TRIGGERED = "watch_triggered"
    TIMER_TRIGGERED = "timer_triggered"
    PERMISSION_RESOLVED = "permission_resolved"
    SYSTEM_NOTIFICATION = "system_notification"


class AgentActorKind(StrEnum):
    USER_SESSION = "user_session"
    CLIENT = "client"
    WATCH_ENGINE = "watch_engine"
    TIMER = "timer"
    BACKEND = "backend"
    SYSTEM = "system"


class AgentInputSource(StrEnum):
    USER = "user"
    SYSTEM = "system"
    BACKEND = "backend"


class AgentInputDeliveryState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class AgentEventKind(StrEnum):
    RUN_STARTED = "run_started"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    PERMISSION_REQUESTED = "permission_requested"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    BACKEND_STATE_CHANGED = "backend_state_changed"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class BackendRuntimeState(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    CONTEXT_LOST = "context_lost"
    RECONCILING = "reconciling"
    CLOSED = "closed"


class AgentInputBase(PayloadModel):
    """Envelope fields shared by every :class:`AgentInput` variant.

    Each variant redeclares ``kind`` as a ``Literal`` so the wire type is a
    discriminated union.
    """

    input_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    actor_id: str = Field(min_length=1, max_length=256)
    actor_kind: AgentActorKind
    auth_epoch: int | None = Field(default=None, ge=0)
    admission_seq: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source: AgentInputSource
    causation_id: str = Field(min_length=1, max_length=256)
    correlation_id: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
    delivery_state: AgentInputDeliveryState = AgentInputDeliveryState.PENDING
    attempt_count: int = Field(default=0, ge=0)


class UserMessagePayload(PayloadModel):
    text: str

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_agent_text(value, max_bytes=MAX_AGENT_TEXT_BYTES)


class WatchTriggeredPayload(PayloadModel):
    watch_id: UUID
    watch_generation: int = Field(ge=0)
    trigger_event_id: UUID
    continuation: str | None = Field(default=None, min_length=1, max_length=4096)
    observation_ref: str | None = Field(default=None, min_length=1, max_length=MAX_REF_LENGTH)


class TimerTriggeredPayload(PayloadModel):
    timer_id: UUID
    scheduled_for: datetime


class PermissionResolvedPayload(PayloadModel):
    approval_request_id: UUID
    decision: ApprovalDecision
    resolved_by_actor_id: str | None = Field(default=None, min_length=1, max_length=256)
    resolved_at: datetime = Field(default_factory=utc_now)


class SystemNotificationPayload(PayloadModel):
    notification_type: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    event_ref: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        # Same strength of constraint as UserMessagePayload.text: a byte cap
        # plus plain-text validation, so notification text can never smuggle
        # control characters into client rendering.
        return validate_plain_text(value, max_bytes=MAX_AGENT_TEXT_BYTES)


class UserMessageInput(AgentInputBase):
    kind: Literal["user_message"]
    payload: UserMessagePayload


class WatchTriggeredInput(AgentInputBase):
    kind: Literal["watch_triggered"]
    payload: WatchTriggeredPayload


class TimerTriggeredInput(AgentInputBase):
    kind: Literal["timer_triggered"]
    payload: TimerTriggeredPayload


class PermissionResolvedInput(AgentInputBase):
    kind: Literal["permission_resolved"]
    payload: PermissionResolvedPayload


class SystemNotificationInput(AgentInputBase):
    kind: Literal["system_notification"]
    payload: SystemNotificationPayload


AgentInput = Annotated[
    UserMessageInput
    | WatchTriggeredInput
    | TimerTriggeredInput
    | PermissionResolvedInput
    | SystemNotificationInput,
    Field(discriminator="kind"),
]


class AgentEventBase(PayloadModel):
    """Envelope fields shared by every :class:`AgentEvent` variant.

    Each variant redeclares ``kind`` as a ``Literal`` so the wire type is a
    discriminated union.
    """

    event_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    run_id: UUID | None = None
    dedup_key: str = Field(min_length=1, max_length=256)
    database_seq: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class RunStartedPayload(PayloadModel):
    input_id: UUID | None = None


class MessageDeltaPayload(PayloadModel):
    message_id: UUID
    part_id: str | None = Field(default=None, min_length=1, max_length=256)
    assembly_revision: int = Field(ge=0)
    text: str | None = Field(default=None, max_length=8192)
    ephemeral: bool = True


class MessageCompletedPayload(PayloadModel):
    message_id: UUID
    assembly_revision: int = Field(ge=0)
    final: bool = True


class ToolStartedPayload(PayloadModel):
    tool_name: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)


class ToolCompletedPayload(PayloadModel):
    tool_name: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    status: ToolResultStatus
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    input_hash: str | None = Field(default=None, min_length=1, max_length=128)
    output_hash: str | None = Field(default=None, min_length=1, max_length=128)
    truncated: bool = False
    error_code: str | None = Field(default=None, min_length=1, max_length=256)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def failed_results_carry_error_code(self) -> ToolCompletedPayload:
        if self.status is ToolResultStatus.ERROR and self.error_code is None:
            raise ValueError("failed tool results require error_code")
        return self


class PermissionRequestedPayload(PayloadModel):
    approval_request_id: UUID
    tool_name: str | None = Field(default=None, min_length=1, max_length=256)
    evidence: str | None = Field(default=None, min_length=1, max_length=4096)
    expires_at: datetime | None = None


class RunCompletedPayload(PayloadModel):
    input_id: UUID | None = None


class RunFailedPayload(PayloadModel):
    error_code: str = Field(min_length=1, max_length=256)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    retryable: bool = False


class BackendStateChangedPayload(PayloadModel):
    state: BackendRuntimeState
    epoch: int = Field(ge=0)


class RunStartedEvent(AgentEventBase):
    kind: Literal["run_started"]
    payload: RunStartedPayload


class MessageDeltaEvent(AgentEventBase):
    kind: Literal["message_delta"]
    payload: MessageDeltaPayload


class MessageCompletedEvent(AgentEventBase):
    kind: Literal["message_completed"]
    payload: MessageCompletedPayload


class ToolStartedEvent(AgentEventBase):
    kind: Literal["tool_started"]
    payload: ToolStartedPayload


class ToolCompletedEvent(AgentEventBase):
    kind: Literal["tool_completed"]
    payload: ToolCompletedPayload


class PermissionRequestedEvent(AgentEventBase):
    kind: Literal["permission_requested"]
    payload: PermissionRequestedPayload


class RunCompletedEvent(AgentEventBase):
    kind: Literal["run_completed"]
    payload: RunCompletedPayload


class RunFailedEvent(AgentEventBase):
    kind: Literal["run_failed"]
    payload: RunFailedPayload


class BackendStateChangedEvent(AgentEventBase):
    kind: Literal["backend_state_changed"]
    payload: BackendStateChangedPayload


AgentEvent = Annotated[
    RunStartedEvent
    | MessageDeltaEvent
    | MessageCompletedEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | PermissionRequestedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | BackendStateChangedEvent,
    Field(discriminator="kind"),
]

_AGENT_INPUT_ADAPTER: TypeAdapter[AgentInput] = TypeAdapter(AgentInput)
_AGENT_EVENT_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


def parse_agent_input(data: Mapping[str, object]) -> AgentInput:
    """Validate a wire ``AgentInput`` mapping into its typed variant."""
    return _AGENT_INPUT_ADAPTER.validate_python(data)


def parse_agent_event(data: Mapping[str, object]) -> AgentEvent:
    """Validate a wire ``AgentEvent`` mapping into its typed variant."""
    return _AGENT_EVENT_ADAPTER.validate_python(data)
