"""Stable terminal tool parameter and result models for the future MCP surface.

These models describe the eight initial TermFlow MCP tools
(:class:`TermFlowToolName`). They are transport-neutral: B owns the security,
policy, and approval checks around the official MCP SDK, while A and B both
enforce the bounds declared here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import utc_now
from .keys import MAX_KEY_SEQUENCE_LENGTH, NAMED_KEYS
from .messages import MAX_TERMINAL_BYTES, validate_plain_text
from .topology import PaneId

# Computer A rejects captures above the terminal payload bound
# (MAX_TERMINAL_BYTES); the MCP read window must never exceed it, or
# every default pane_read is refused with 'capture ceiling' on A.
MAX_PANE_READ_BYTES = MAX_TERMINAL_BYTES
MAX_PANE_READ_LINES = 1_000_000
MAX_WATCH_MATCH_LENGTH = 1024


class TermFlowToolName(StrEnum):
    LIST_PANES = "termflow_list_panes"
    PANE_READ = "termflow_pane_read"
    PANE_SEND_TEXT = "termflow_pane_send_text"
    PANE_SEND_KEYS = "termflow_pane_send_keys"
    WATCH_CREATE = "termflow_watch_create"
    WATCH_LIST = "termflow_watch_list"
    WATCH_GET = "termflow_watch_get"
    WATCH_CANCEL = "termflow_watch_cancel"


class TermFlowErrorCode(StrEnum):
    PANE_NOT_FOUND = "pane_not_found"
    INCARNATION_CHANGED = "incarnation_changed"
    POLICY_DENIED = "policy_denied"
    STREAM_GAP = "stream_gap"
    QUOTA_EXCEEDED = "quota_exceeded"
    INVALID_REQUEST = "invalid_request"
    WATCH_NOT_FOUND = "watch_not_found"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_CONFLICT = "approval_conflict"
    OUTCOME_UNKNOWN = "outcome_unknown"
    INTERNAL_ERROR = "internal_error"


class PaneReadView(StrEnum):
    VIEWPORT = "viewport"
    HISTORY = "history"
    SINCE = "since"


class WatchConditionKind(StrEnum):
    OUTPUT_CONTAINS = "output_contains"
    OUTPUT_IDLE = "output_idle"
    PANE_EXITED = "pane_exited"


class WatchStatus(StrEnum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaneCursor(ToolModel):
    """The verified observation cursor used for ``since`` reads and watches."""

    instance_id: UUID
    pane_id: PaneId
    pane_incarnation: int = Field(ge=0)
    stream_id: UUID
    seq: int = Field(ge=0)


class PaneReadParams(ToolModel):
    pane_id: PaneId
    instance_id: UUID | None = None
    view: PaneReadView
    start_line: int | None = Field(default=None, ge=0, le=MAX_PANE_READ_LINES)
    end_line: int | None = Field(default=None, ge=0, le=MAX_PANE_READ_LINES)
    tail_lines: int | None = Field(default=None, ge=1, le=MAX_PANE_READ_LINES)
    max_bytes: int = Field(default=MAX_PANE_READ_BYTES, ge=1, le=MAX_PANE_READ_BYTES)
    join_wrapped: bool = False
    cursor: PaneCursor | None = None

    @model_validator(mode="after")
    def range_is_consistent(self) -> PaneReadParams:
        if self.view is PaneReadView.SINCE:
            if self.cursor is None:
                raise ValueError("cursor is required when view is 'since'")
            if (
                self.start_line is not None
                or self.end_line is not None
                or self.tail_lines is not None
            ):
                raise ValueError("line ranges are not valid when view is 'since'")
        elif self.cursor is not None:
            raise ValueError("cursor is only valid when view is 'since'")
        if (
            self.tail_lines is not None
            and (self.start_line is not None or self.end_line is not None)
        ):
            raise ValueError("tail_lines cannot be combined with start_line or end_line")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.start_line > self.end_line
        ):
            raise ValueError("start_line must not exceed end_line")
        return self


class PaneSendTextParams(ToolModel):
    pane_id: PaneId
    instance_id: UUID | None = None
    request_key: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Stable idempotency key for this exact write inside the conversation. "
            "Reuse it only to retry the same write: the retry observes the original "
            "receipt, pending state, or rejection instead of executing twice. A new "
            "write must use a new key."
        ),
    )
    conversation_id: UUID
    intent: str | None = Field(default=None, min_length=1, max_length=1024)
    text: str
    submit: bool = False

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_plain_text(value)


class PaneSendKeysParams(ToolModel):
    pane_id: PaneId
    instance_id: UUID | None = None
    request_key: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Stable idempotency key for this exact write inside the conversation. "
            "Reuse it only to retry the same write: the retry observes the original "
            "receipt, pending state, or rejection instead of executing twice. A new "
            "write must use a new key."
        ),
    )
    conversation_id: UUID
    intent: str | None = Field(default=None, min_length=1, max_length=1024)
    keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_KEY_SEQUENCE_LENGTH)

    @field_validator("keys")
    @classmethod
    def named_keys_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # The closed NAMED_KEYS vocabulary is shared by B, the wire protocol,
        # and A: membership is the single validation so no side can drift.
        unknown = [key for key in value if key not in NAMED_KEYS]
        if unknown:
            raise ValueError(f"unknown named keys: {sorted(unknown)}")
        return value


class WatchCondition(ToolModel):
    kind: WatchConditionKind
    match: str | None = Field(default=None, min_length=1, max_length=MAX_WATCH_MATCH_LENGTH)
    idle_after_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def condition_is_consistent(self) -> WatchCondition:
        if self.kind is WatchConditionKind.OUTPUT_CONTAINS and not self.match:
            raise ValueError("output_contains requires a bounded literal match")
        if self.kind is WatchConditionKind.OUTPUT_IDLE and self.idle_after_seconds is None:
            raise ValueError("output_idle requires idle_after_seconds")
        if self.kind is WatchConditionKind.PANE_EXITED and self.match is not None:
            raise ValueError("pane_exited does not accept a match")
        return self


class WatchCreateParams(ToolModel):
    pane_id: PaneId
    instance_id: UUID | None = None
    conversation_id: UUID
    condition: WatchCondition
    start_cursor: PaneCursor | None = None
    expires_at: datetime | None = None
    one_shot: bool = True
    max_rearms: int | None = Field(default=None, ge=1)
    intent: str | None = Field(default=None, min_length=1, max_length=MAX_WATCH_MATCH_LENGTH)
    proposed_action: str | None = Field(
        default=None, min_length=1, max_length=MAX_WATCH_MATCH_LENGTH
    )

    @model_validator(mode="after")
    def rearm_policy_is_bounded(self) -> WatchCreateParams:
        if not self.one_shot and self.max_rearms is None:
            raise ValueError("max_rearms is required for rearmable watches")
        return self


class WatchListParams(ToolModel):
    conversation_id: UUID | None = None
    status: WatchStatus | None = None


class WatchGetParams(ToolModel):
    watch_id: UUID


class WatchCancelParams(ToolModel):
    watch_id: UUID


class PaneSummary(ToolModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pane_id: PaneId
    index: int = Field(ge=0)
    title: str | None = None
    active: bool
    dead: bool


class ListPanesResult(ToolModel):
    instance_id: UUID
    panes: list[PaneSummary]


class StreamGapInfo(ToolModel):
    pane_id: PaneId
    previous_stream_id: UUID
    reason: Literal["stream_changed", "overwritten", "backpressure", "control_paused"]
    recovered_cursor: PaneCursor | None = None


class PaneReadResult(ToolModel):
    instance_id: UUID
    pane_id: PaneId
    view: PaneReadView
    content: str
    encoding: Literal["utf-8"] = "utf-8"
    stream_id: UUID | None = None
    from_seq: int | None = Field(default=None, ge=0)
    to_seq: int | None = Field(default=None, ge=0)
    captured_start_line: int | None = Field(default=None, ge=0)
    captured_end_line: int | None = Field(default=None, ge=0)
    truncated: bool = False
    stream_gap: StreamGapInfo | None = None


class PaneWriteResult(ToolModel):
    request_key: str = Field(min_length=1, max_length=128)
    ok: bool
    outcome: Literal["confirmed", "outcome_unknown", "failed"]
    error_code: TermFlowErrorCode | None = None
    #: The single-use approval that authorized this write, when one exists
    #: (always set by the M5.2 approval-gated path; null keeps older receipts
    #: backward compatible).
    approval_id: UUID | None = None

    @model_validator(mode="after")
    def result_is_consistent(self) -> PaneWriteResult:
        if self.ok != (self.outcome == "confirmed"):
            raise ValueError("ok must be true exactly when outcome is 'confirmed'")
        if not self.ok and self.error_code is None:
            raise ValueError("failed results require error_code")
        return self


class PaneSendTextResult(PaneWriteResult):
    """Receipt for :attr:`TermFlowToolName.PANE_SEND_TEXT`."""


class PaneSendKeysResult(PaneWriteResult):
    """Receipt for :attr:`TermFlowToolName.PANE_SEND_KEYS`."""


class WatchCreateResult(ToolModel):
    watch_id: UUID
    generation: int = Field(ge=0)
    start_cursor: PaneCursor | None = None
    expires_at: datetime | None = None


class WatchSummary(ToolModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    watch_id: UUID
    conversation_id: UUID
    instance_id: UUID
    pane_id: PaneId
    condition: WatchCondition
    status: WatchStatus
    generation: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class WatchDetail(WatchSummary):
    start_cursor: PaneCursor | None = None
    rearm_cursor: PaneCursor | None = None
    expires_at: datetime | None = None
    one_shot: bool = True
    max_rearms: int | None = Field(default=None, ge=1)
    attempt_count: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, min_length=1, max_length=MAX_WATCH_MATCH_LENGTH)
    next_attempt_at: datetime | None = None


class WatchListResult(ToolModel):
    watches: list[WatchSummary]


class WatchGetResult(ToolModel):
    watch: WatchDetail


class WatchCancelResult(ToolModel):
    watch_id: UUID
    ok: bool
    error_code: TermFlowErrorCode | None = None

    @model_validator(mode="after")
    def result_is_consistent(self) -> WatchCancelResult:
        if self.ok and self.error_code is not None:
            raise ValueError("successful results cannot contain error_code")
        if not self.ok and self.error_code is None:
            raise ValueError("failed results require error_code")
        return self
