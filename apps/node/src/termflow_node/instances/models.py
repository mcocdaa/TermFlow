"""Local metadata for one isolated tmux-backed Instance."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator


class InstanceLifecycle(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    BROKEN = "broken"


class RemoteAccessState(StrEnum):
    ACTIVE = "active"
    ACTIVATION_REQUIRED = "activation_required"


class LocalInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2, 3] = 1
    instance_id: UUID
    name: str
    session_id: str | None = None
    session_name: str = "main"
    socket_path: Path
    created_at: datetime
    bridge_pid: int | None = None
    instance_token: SecretStr | None = None
    lifecycle: InstanceLifecycle
    remote_access: RemoteAccessState = RemoteAccessState.ACTIVE

    @model_validator(mode="after")
    def stable_identity_matches_schema(self) -> "LocalInstance":
        if self.session_id is not None and not (
            self.session_id.startswith("$") and self.session_id[1:].isdigit()
        ):
            raise ValueError("session_id must be a stable tmux Session ID")
        if self.schema_version in {2, 3} and self.session_id is None:
            raise ValueError(f"schema version {self.schema_version} requires session_id")
        return self
