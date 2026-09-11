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

#: One ``agent_mcp_allowed_hosts`` entry: a hostname/IPv4 (or bracketed IPv6)
#: with an optional ``:port`` or ``:*`` suffix.  Mirrors the MCP SDK's
#: DNS-rebinding matcher, which does exact matches and ``host:*`` wildcards
#: (no host wildcards, no schemes).
_ALLOWED_HOST_ENTRY = re.compile(
    r"^(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])"
    r"(?::(?P<port>\*|[0-9]{1,5}))?$"
)


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
    enable_docs: bool = False
    agent_broker_enabled: bool = True
    trust_proxy: bool = False
    static_dir: Path = Path("/app/frontend-dist")
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    trusted_web_origins: Annotated[tuple[str, ...], NoDecode] = ()
    # Agent Broker MCP server (plan §10): the deployment-owned OpenCode
    # config path feeding the startup drift guard, plus the transport bounds
    # for the observe-only MCP endpoint.  Defaults mirror the SDK-facing
    # constants in plugins.agent_broker.api.mcp_server
    # (DEFAULT_ALLOWED_HOSTS / DEFAULT_MAX_REQUEST_BYTES).
    opencode_config_path: str | None = None
    # The MCP Streamable HTTP endpoint's DNS-rebinding allowlist.  The
    # loopback-only default serves non-container deployments; a containerized
    # OpenCode that reaches B over the agent_internal network must name B's
    # internal host explicitly (e.g. ``control-plane:8000`` in the reference
    # compose profile, plan §16) or every MCP request is rejected with 421.
    # Entries are ``host``, ``host:port``, or ``host:*`` (bracketed IPv6
    # allowed); the SDK matches them exactly or by the ``host:*`` wildcard.
    agent_mcp_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = (
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    )
    agent_mcp_max_request_bytes: int = Field(default=256 * 1024, ge=1)
    # M5.2 approval flow: the write tools wait synchronously for the human
    # decision (must stay below the MCP tool guard timeout) and created
    # approvals expire after the TTL.
    #: Synchronous approval wait budget.  Kept below the pinned OpenCode MCP
    #: client's own call timeout so the handler can hand a still-pending
    #: request to the background late-completion path before the client
    #: cancels the call.
    agent_approval_wait_timeout_seconds: float = Field(default=45.0, gt=0)
    agent_approval_ttl_seconds: float = Field(default=300.0, gt=0)
    #: MCP tool-call budget; must stay above ``agent_approval_wait_timeout_seconds``.
    agent_tool_timeout_seconds: float = Field(default=60.0, gt=0)
    # Delegated Write Grants stay disabled in 0.2.0: no code path reads the
    # table, the canonical hash always binds grant_id=None, and this flag is
    # only exposed for C to display (spec §8).
    agent_delegated_write_grants_enabled: bool = False
    # M4.5 agent pipeline (spec §2): the single-binding reference deployment's
    # OpenCode runtime endpoint (compose agent_internal network) and workspace
    # directory.  Multi-binding fleets resolve endpoints through a deployment
    # endpoint provider injected into AgentRuntimeRegistry; the base_url/
    # directory defaults keep the reference deployment runnable with no extra
    # configuration.  reconcile_attempts bounds the SSE disconnect reconcile
    # retry loop.
    agent_opencode_base_url: str = "http://opencode-agent:4096"
    agent_opencode_directory: str = "/workspace"
    # HTTP Basic auth pair for the internal B-to-OpenCode connection
    # (compose OPENCODE_SERVER_*; M4 exit verified both the container side
    # and the adapter client-level auth). Both or neither must be set.
    agent_opencode_username: str | None = None
    agent_opencode_password: SecretStr | None = None
    # 2026-08-22 scope decision: the production runtime gate is an
    # authenticated /global/health probe (http_runtime_client); deployment
    # tooling owns container lifecycle. This capability secret backs the
    # supervisor restart ceremony only; it is shared with the container
    # through OpenCode's native {env:} config interpolation and is never
    # persisted by B.
    agent_opencode_mcp_token: SecretStr | None = None
    agent_cleanup_helper_token: SecretStr | None = None
    # Server-owned DeepSeek provider catalog.  Endpoint/model/source names
    # have reference defaults, but policy claims do not: a catalog entry is
    # activatable only when the deployment owner explicitly supplies complete
    # retention/region metadata and verifies ``no_training=true``.
    agent_provider_deepseek_endpoint_origin: str = "https://api.deepseek.com"
    agent_provider_deepseek_model_ids: Annotated[tuple[str, ...], NoDecode] = (
        "deepseek-v4-flash",
    )
    agent_provider_deepseek_region: str | None = None
    agent_provider_deepseek_retention_terms: str | None = None
    agent_provider_deepseek_retention_version: str | None = None
    agent_provider_deepseek_no_training: bool | None = None
    # This is an environment-variable/source label, never the credential.
    agent_provider_deepseek_credential_source: str | None = "OPENAI_API_KEY"
    agent_provider_deepseek_policy_version: str | None = None
    agent_pipeline_reconcile_attempts: int = Field(default=5, ge=1)
    # M4.5 watch wiring (spec §3a): the lifespan watch deadline task sweeps due
    # ``output_idle`` deadlines every tick and hands each FiredTrigger to its
    # binding pipeline.  Tests inject a short period (or drive the tick
    # manually) to avoid real-time waits.
    agent_watch_deadline_tick_seconds: float = Field(default=1.0, gt=0)
    # Runtime-controller health/reconciliation sweep.  A short bounded tick
    # detects OpenCode/MCP drift without coupling the core terminal heartbeat
    # to Agent availability.
    agent_runtime_health_tick_seconds: float = Field(default=5.0, gt=0)
    browser_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60)
    browser_session_capacity: int = Field(default=4096, ge=1)
    # Sensitive administrator actions must be backed by a recent strong
    # authentication.  Keep the deployment knob bounded by the contract so a
    # misconfigured instance cannot silently widen the freshness window.
    agent_sensitive_action_max_age_seconds: int = Field(default=300, ge=0, le=300)
    totp_master_key: SecretStr | None = None
    totp_master_key_file: Path | None = None
    totp_auto_master_key_file: Path | None = None
    totp_master_key_version: int = Field(default=1, ge=1, le=2_147_483_647)
    totp_setup_ttl_seconds: int = Field(default=10 * 60, ge=60, le=60 * 60)
    auth_challenge_ttl_seconds: int = Field(default=5 * 60, ge=30, le=15 * 60)
    oauth_authorization_ttl_seconds: int = Field(default=5 * 60, ge=30, le=15 * 60)
    oauth_authorization_code_ttl_seconds: int = Field(default=60, ge=30, le=120)
    oauth_device_authorization_ttl_seconds: int = Field(default=15 * 60, ge=60, le=30 * 60)
    oauth_device_poll_interval_seconds: int = Field(default=5, ge=1, le=60)
    auth_access_token_ttl_seconds: int = Field(default=10 * 60, ge=60, le=60 * 60)
    auth_refresh_token_ttl_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        ge=60 * 60,
        le=90 * 24 * 60 * 60,
    )
    auth_cli_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    auth_attempt_budget_capacity: int = Field(default=5, ge=1, le=100)
    auth_attempt_refill_seconds: int = Field(default=60, ge=1, le=60 * 60)
    oauth_device_poll_budget_capacity: int = Field(default=60, ge=1, le=600)
    oauth_device_poll_budget_refill_seconds: int = Field(default=60, ge=1, le=60 * 60)
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

    @field_validator("agent_mcp_allowed_hosts", mode="before")
    @classmethod
    def parse_agent_mcp_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("agent_provider_deepseek_model_ids", mode="before")
    @classmethod
    def parse_agent_provider_model_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("agent_provider_deepseek_endpoint_origin")
    @classmethod
    def validate_agent_provider_endpoint_origin(cls, value: str) -> str:
        return _web_origin(value)

    @field_validator("agent_mcp_allowed_hosts")
    @classmethod
    def validate_agent_mcp_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            # An empty allowlist would make the SDK reject every MCP request
            # (421) silently; that is a misconfiguration, not a safe default.
            raise ValueError("agent_mcp_allowed_hosts must not be empty")
        for entry in value:
            match = _ALLOWED_HOST_ENTRY.fullmatch(entry)
            if match is None:
                raise ValueError(
                    f"agent MCP allowed host {entry!r} must be 'host', "
                    "'host:port', or 'host:*' (bracketed IPv6 allowed)"
                )
            port = match.group("port")
            if port is not None and port != "*" and not 1 <= int(port) <= 65535:
                raise ValueError(f"agent MCP allowed host {entry!r} has an invalid port")
        return value

    @field_validator(
        "agent_opencode_username",
        "agent_opencode_password",
        mode="before",
    )
    @classmethod
    def normalize_empty_opencode_basic_auth(cls, value: object) -> object:
        # Compose passes the unset pair as empty strings. Treat each empty
        # value as absent so a partial/blank pair fails the combined validator
        # and a fully empty pair becomes unconfigured.
        if isinstance(value, str) and value == "":
            return None
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
        if (self.agent_opencode_username is None) != (self.agent_opencode_password is None):
            raise ValueError(
                "agent_opencode_username and agent_opencode_password must be set together"
            )
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
