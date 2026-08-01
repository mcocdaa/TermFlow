"""Environment-backed Control Plane settings."""

from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _web_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web origins must be absolute HTTP(S) origins")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("web origins cannot contain credentials, query, or fragment")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMFLOW_", extra="ignore")

    admin_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/termflow.db"
    allow_insecure_loopback: bool = False
    static_dir: Path = Path("/app/frontend-dist")
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    trusted_web_origins: Annotated[tuple[str, ...], NoDecode] = ()
    browser_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60)
    browser_session_capacity: int = Field(default=4096, ge=1)
    terminal_max_frame_bytes: int = Field(default=65_536, ge=1, le=65_536)
    terminal_input_rate_bytes_per_second: int = Field(default=256 * 1024, ge=1)
    terminal_queue_max_messages: int = Field(default=256, ge=1)
    terminal_queue_max_bytes: int = Field(default=1024 * 1024, ge=1)
    terminal_resume_grace_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    heartbeat_interval_seconds: int = 15
    offline_after_seconds: int = 45
    command_timeout_seconds: float = 5.0
    connection_queue_size: int = 256
    event_queue_size: int = 512
    max_input_bytes: int = 16 * 1024
    @field_validator("trusted_web_origins", mode="before")
    @classmethod
    def parse_trusted_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("trusted_web_origins")
    @classmethod
    def validate_trusted_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_web_origin(origin) for origin in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("trusted_web_origins must be unique")
        return normalized

    @model_validator(mode="after")
    def offline_timeout_exceeds_heartbeat(self) -> "Settings":
        if self.offline_after_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("offline_after_seconds must exceed heartbeat_interval_seconds")
        return self
    @property
    def allowed_web_origins(self) -> tuple[str, ...]:
        return self.trusted_web_origins or (_web_origin(str(self.public_base_url)),)
