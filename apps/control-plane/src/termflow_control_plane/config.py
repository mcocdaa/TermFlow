"""Environment-backed Control Plane settings."""

from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMFLOW_", extra="ignore")

    admin_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/termflow.db"
    allow_insecure_loopback: bool = False
    heartbeat_interval_seconds: int = 15
    offline_after_seconds: int = 45
    command_timeout_seconds: float = 5.0
    connection_queue_size: int = 256
    event_queue_size: int = 512
    max_input_bytes: int = 16 * 1024
    static_dir: Path = Path("/app/frontend-dist")

    @model_validator(mode="after")
    def offline_timeout_exceeds_heartbeat(self) -> "Settings":
        if self.offline_after_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("offline_after_seconds must exceed heartbeat_interval_seconds")
        return self
