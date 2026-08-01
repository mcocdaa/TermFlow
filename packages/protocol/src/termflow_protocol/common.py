"""Versioned envelope shared by every TermFlow transport."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


class MessageType(StrEnum):
    BRIDGE_HELLO = "bridge.hello"
    BRIDGE_HEARTBEAT = "bridge.heartbeat"
    TOPOLOGY_SNAPSHOT = "topology.snapshot"
    TOPOLOGY_CHANGED = "topology.changed"
    PANE_OUTPUT = "pane.output"
    PANE_INPUT = "pane.input"
    PANE_REPLAY_REQUEST = "pane.replay_request"
    STREAM_GAP = "stream.gap"
    COMMAND_RESULT = "command.result"
    INSTANCE_ONLINE = "instance.online"
    INSTANCE_OFFLINE = "instance.offline"
    TERMINAL_OPEN = "terminal.open"
    TERMINAL_OPENED = "terminal.opened"
    TERMINAL_INPUT = "terminal.input"
    TERMINAL_OUTPUT = "terminal.output"
    TERMINAL_SIZE = "terminal.size"
    TERMINAL_BINDINGS = "terminal.bindings"
    TERMINAL_ACTION = "terminal.action"
    TERMINAL_ACTION_RESULT = "terminal.action_result"
    TERMINAL_CLOSE = "terminal.close"
    TERMINAL_CLOSED = "terminal.closed"
    TERM_RENAME = "term.rename"
    TERM_RENAME_RESULT = "term.rename_result"


class WireMessage(BaseModel):
    """Transport-neutral V1 message envelope."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    message_id: UUID = Field(default_factory=uuid4)
    type: MessageType
    instance_id: UUID
    sent_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, object]
