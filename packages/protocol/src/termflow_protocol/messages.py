"""Typed payloads carried inside :class:`WireMessage`."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import utc_now
from .topology import PaneId, TopologySnapshot

MAX_INPUT_BYTES = 16 * 1024


def validate_plain_text(text: str, *, max_bytes: int = MAX_INPUT_BYTES) -> str:
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in text):
        raise ValueError("text contains unsupported control characters")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"text exceeds {max_bytes} UTF-8 bytes")
    return text


class PayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BridgeHelloPayload(PayloadModel):
    protocol_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=128)
    capabilities: tuple[str, ...] = ("plain_text_input", "topology", "pane_output")


class BridgeHeartbeatPayload(PayloadModel):
    observed_at: datetime = Field(default_factory=utc_now)


class TopologySnapshotPayload(PayloadModel):
    topology: TopologySnapshot


class TopologyChangedPayload(PayloadModel):
    topology: TopologySnapshot


class PaneOutputPayload(PayloadModel):
    pane_id: PaneId
    stream_id: UUID
    seq: int = Field(ge=1)
    data_base64: str
    captured_at: datetime = Field(default_factory=utc_now)

    @field_validator("data_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data_base64 must be strict Base64") from exc
        return value

    @classmethod
    def from_bytes(
        cls,
        pane_id: str,
        stream_id: UUID,
        seq: int,
        data: bytes,
    ) -> PaneOutputPayload:
        return cls(
            pane_id=pane_id,
            stream_id=stream_id,
            seq=seq,
            data_base64=base64.b64encode(data).decode("ascii"),
        )

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)


class PaneInputPayload(PayloadModel):
    command_id: UUID
    idempotency_key: UUID
    pane_id: PaneId
    text: str
    submit: bool

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_plain_text(value)

    @model_validator(mode="after")
    def has_effect(self) -> PaneInputPayload:
        if not self.text and not self.submit:
            raise ValueError("text must be non-empty unless submit is true")
        return self


class PaneReplayRequestPayload(PayloadModel):
    pane_id: PaneId
    stream_id: UUID
    after_seq: int = Field(ge=0)


class StreamGapPayload(PayloadModel):
    pane_id: PaneId
    previous_stream_id: UUID
    reason: Literal["stream_changed", "overwritten", "backpressure", "control_paused"]


class CommandResultPayload(PayloadModel):
    command_id: UUID
    idempotency_key: UUID
    ok: bool
    error_code: str | None = None

    @model_validator(mode="after")
    def result_is_consistent(self) -> CommandResultPayload:
        if self.ok and self.error_code is not None:
            raise ValueError("successful results cannot contain error_code")
        if not self.ok and not self.error_code:
            raise ValueError("failed results require error_code")
        return self


class InstancePresencePayload(PayloadModel):
    status: Literal["online", "offline"]
    observed_at: datetime = Field(default_factory=utc_now)


PAYLOAD_MODELS: dict[str, type[PayloadModel]] = {
    "bridge.hello": BridgeHelloPayload,
    "bridge.heartbeat": BridgeHeartbeatPayload,
    "topology.snapshot": TopologySnapshotPayload,
    "topology.changed": TopologyChangedPayload,
    "pane.output": PaneOutputPayload,
    "pane.input": PaneInputPayload,
    "pane.replay_request": PaneReplayRequestPayload,
    "stream.gap": StreamGapPayload,
    "command.result": CommandResultPayload,
    "instance.online": InstancePresencePayload,
    "instance.offline": InstancePresencePayload,
}


def parse_payload(message_type: str, payload: dict[str, object]) -> PayloadModel:
    model = PAYLOAD_MODELS.get(message_type)
    if model is None:
        raise ValueError(f"unsupported message type: {message_type}")
    return model.model_validate(payload)
