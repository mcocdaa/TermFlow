"""Environment-backed Control Plane settings."""

import base64
import binascii
import re
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


_UNPADDED_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _decode_master_key(value: SecretStr) -> bytes:
    encoded = value.get_secret_value()
    if "=" in encoded or not _UNPADDED_BASE64URL.fullmatch(encoded):
        raise ValueError("TOTP master key must use unpadded base64url")
    try:
        decoded = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("TOTP master key must use unpadded base64url") from exc
    if len(decoded) != 32:
        raise ValueError("TOTP master key must decode to exactly 32 bytes")
    return decoded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TERMFLOW_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    admin_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/termflow.db"
    allow_insecure_loopback: bool = False
    static_dir: Path = Path("/app/frontend-dist")
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    trusted_web_origins: Annotated[tuple[str, ...], NoDecode] = ()
    browser_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60)
    browser_session_capacity: int = Field(default=4096, ge=1)
    totp_master_key: SecretStr | None = None
    totp_master_key_file: Path | None = None
    totp_master_key_version: int = Field(default=1, ge=1, le=2_147_483_647)
    totp_setup_ttl_seconds: int = Field(default=10 * 60, ge=60, le=60 * 60)
    auth_challenge_ttl_seconds: int = Field(default=5 * 60, ge=30, le=15 * 60)
    oauth_authorization_ttl_seconds: int = Field(default=5 * 60, ge=30, le=15 * 60)
    oauth_authorization_code_ttl_seconds: int = Field(default=60, ge=30, le=120)
    auth_access_token_ttl_seconds: int = Field(default=10 * 60, ge=60, le=60 * 60)
    auth_refresh_token_ttl_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        ge=60 * 60,
        le=90 * 24 * 60 * 60,
    )
    auth_cli_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    auth_attempt_budget_capacity: int = Field(default=5, ge=1, le=100)
    auth_attempt_refill_seconds: int = Field(default=60, ge=1, le=60 * 60)
    auth_max_challenge_attempts: int = Field(default=5, ge=1, le=10)
    auth_max_backoff_seconds: int = Field(default=5 * 60, ge=1, le=60 * 60)
    auth_global_verification_capacity: int = Field(default=32, ge=1, le=1024)
    enrollment_token_ttl_seconds: int = Field(default=60, ge=10, le=600)
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

    @field_validator("admin_token")
    @classmethod
    def validate_admin_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("administrator token must contain at least 32 UTF-8 bytes")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("public_base_url cannot contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("public_base_url must not contain a path")
        return value

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
    def validate_combined_settings(self) -> "Settings":
        if self.offline_after_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("offline_after_seconds must exceed heartbeat_interval_seconds")
        if self.totp_master_key is not None and self.totp_master_key_file is not None:
            raise ValueError("configure only one TOTP master key source")
        if self.totp_master_key is not None:
            _decode_master_key(self.totp_master_key)
        if self.totp_master_key_file is not None:
            try:
                encoded = SecretStr(self.totp_master_key_file.read_text(encoding="utf-8").strip())
            except OSError as exc:
                raise ValueError("TOTP master key file cannot be read") from exc
            _decode_master_key(encoded)
        return self

    @property
    def allowed_web_origins(self) -> tuple[str, ...]:
        return self.trusted_web_origins or (_web_origin(str(self.public_base_url)),)

    @property
    def totp_master_key_bytes(self) -> bytes | None:
        if self.totp_master_key is not None:
            return _decode_master_key(self.totp_master_key)
        if self.totp_master_key_file is None:
            return None
        try:
            encoded = SecretStr(self.totp_master_key_file.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise ValueError("TOTP master key file cannot be read") from exc
        return _decode_master_key(encoded)
