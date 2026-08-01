"""HTTP request and response DTOs for TermFlow V1."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .messages import TerminalAction, TerminalBinding, TerminalCloseReason, validate_plain_text
from .topology import TopologySnapshot


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_editable_name(value: str) -> str:
    if not 1 <= len(value) <= 128:
        raise ValueError("name must contain between 1 and 128 characters")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise ValueError("name contains unsupported control characters")
    return value


class PaneInputRequest(HttpModel):
    text: str
    submit: bool

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_plain_text(value)

    @model_validator(mode="after")
    def has_effect(self) -> "PaneInputRequest":
        if not self.text and not self.submit:
            raise ValueError("text must be non-empty unless submit is true")
        return self


class ErrorDetail(HttpModel):
    code: str
    message: str
    request_id: UUID


class ErrorEnvelope(HttpModel):
    error: ErrorDetail


class EnrollmentCreateRequest(HttpModel):
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str | None) -> str | None:
        return validate_editable_name(value) if value is not None else None


class EnrollmentCreateResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime


class EnrollmentMetadataResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime
    login_command: str = Field(repr=False, min_length=1)


class InstallationEnrollRequest(HttpModel):
    enrollment_token: SecretStr
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    client_version: str | None = Field(default=None, min_length=1, max_length=64)


class InstallationEnrollResponse(HttpModel):
    installation_id: UUID
    installation_token: str = Field(repr=False, min_length=32)


class InstanceRegisterRequest(HttpModel):
    instance_id: UUID
    name: str = Field(min_length=1, max_length=128)


class InstanceRegisterResponse(HttpModel):
    instance_id: UUID
    instance_token: str = Field(repr=False, min_length=32)


class InstanceResponse(HttpModel):
    instance_id: UUID
    name: str
    installation_id: UUID
    created_at: datetime
    online: bool


class InstanceListResponse(HttpModel):
    instances: list[InstanceResponse]


class TopologyResponse(HttpModel):
    instance_id: UUID
    topology: TopologySnapshot


class CommandResponse(HttpModel):
    command_id: UUID
    idempotency_key: UUID
    ok: bool


class HealthResponse(HttpModel):
    status: str = "ok"


class BrowserSessionCreateRequest(HttpModel):
    admin_token: SecretStr


class BrowserSessionResponse(HttpModel):
    authenticated: bool = True
    expires_at: datetime


class BrowserSessionDeleteResponse(HttpModel):
    ok: bool = True


class ComputerRenameRequest(HttpModel):
    display_name: str

    @field_validator("display_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return validate_editable_name(value)


class TermRenameRequest(HttpModel):
    name: str

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return validate_editable_name(value)


class TermSummary(HttpModel):
    instance_id: UUID
    name: str
    online: bool
    window_count: int = Field(ge=0)
    pane_count: int = Field(ge=0)
    active_pane_count: int = Field(ge=0)
    current_command: str | None = None
    last_seen_at: datetime | None = None


class ComputerSummary(HttpModel):
    installation_id: UUID
    hostname: str | None = None
    display_name: str
    platform: str | None = None
    client_version: str | None = None
    registered_at: datetime | None = None
    last_seen_at: datetime | None = None
    online: bool
    terms: list[TermSummary]


class ComputerListResponse(HttpModel):
    computers: list[ComputerSummary]


class DashboardMetrics(HttpModel):
    online_terms: int = Field(ge=0)
    total_terms: int = Field(ge=0)
    active_panes: int = Field(ge=0)
    interactions_24h: int = Field(ge=0)
    computers: int = Field(ge=0)


class DashboardResponse(HttpModel):
    metrics: DashboardMetrics
    computers: list[ComputerSummary]


class TerminalReadyFrame(HttpModel):
    type: Literal["terminal.ready"] = "terminal.ready"
    terminal_id: UUID
    stream_id: UUID
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)


class TerminalSizeFrame(HttpModel):
    type: Literal["terminal.size"] = "terminal.size"
    terminal_id: UUID
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)


class TerminalBindingSnapshotFrame(HttpModel):
    type: Literal["terminal.binding_snapshot"] = "terminal.binding_snapshot"
    terminal_id: UUID
    prefix: str
    prefix2: str | None = None
    bindings: list[TerminalBinding]


class TerminalErrorFrame(HttpModel):
    type: Literal["terminal.error"] = "terminal.error"
    terminal_id: UUID
    code: str
    message: str


class TerminalClosedFrame(HttpModel):
    type: Literal["terminal.closed"] = "terminal.closed"
    terminal_id: UUID
    reason: TerminalCloseReason


class TerminalActionResultFrame(HttpModel):
    type: Literal["terminal.action_result"] = "terminal.action_result"
    terminal_id: UUID
    action_id: UUID
    ok: bool
    error_code: str | None = None


class TerminalActionFrame(HttpModel):
    type: Literal["terminal.action"] = "terminal.action"
    action_id: UUID
    action: TerminalAction
    target_pane_id: str | None = None
    confirmed: bool = False


class TerminalCloseFrame(HttpModel):
    type: Literal["terminal.close"] = "terminal.close"
    reason: Literal["client_closed"] = "client_closed"
