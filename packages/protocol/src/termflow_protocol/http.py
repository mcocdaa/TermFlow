"""HTTP request and response DTOs for TermFlow V1."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .messages import validate_plain_text
from .topology import TopologySnapshot


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class EnrollmentCreateResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime


class InstallationEnrollRequest(HttpModel):
    enrollment_token: SecretStr


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
