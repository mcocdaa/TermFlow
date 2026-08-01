"""Local metadata for one isolated tmux-backed Instance."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr


class InstanceLifecycle(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    BROKEN = "broken"


class LocalInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: UUID
    name: str
    session_name: Literal["main"] = "main"
    socket_path: Path
    created_at: datetime
    bridge_pid: int | None = None
    instance_token: SecretStr | None = None
    lifecycle: InstanceLifecycle
