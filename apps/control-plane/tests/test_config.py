import base64

import pytest
from pydantic import ValidationError
from termflow_control_plane.config import Settings

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"


def test_offline_timeout_must_exceed_heartbeat() -> None:
    with pytest.raises(ValidationError):
        Settings(
            admin_token=ADMIN_TOKEN,
            heartbeat_interval_seconds=15,
            offline_after_seconds=15,
        )


def test_control_plane_defaults_are_single_process_friendly() -> None:
    settings = Settings(admin_token=ADMIN_TOKEN)
    assert settings.connection_queue_size == 256
    assert settings.event_queue_size == 512
    assert settings.max_input_bytes == 16 * 1024
    assert settings.browser_session_ttl_seconds == 8 * 60 * 60
    assert settings.enrollment_token_ttl_seconds == 60
    assert settings.terminal_max_frame_bytes == 65_536
    assert settings.terminal_input_rate_bytes_per_second == 256 * 1024
    assert settings.terminal_queue_max_messages == 256
    assert settings.terminal_queue_max_bytes == 1024 * 1024
    assert settings.terminal_resume_grace_seconds == 30
    assert settings.allowed_web_origins == ("http://127.0.0.1:8000",)
    assert settings.auth_challenge_ttl_seconds == 5 * 60
    assert settings.oauth_authorization_ttl_seconds == 5 * 60
    assert settings.oauth_authorization_code_ttl_seconds == 60
    assert settings.auth_access_token_ttl_seconds == 10 * 60
    assert settings.auth_refresh_token_ttl_seconds == 30 * 24 * 60 * 60
    assert settings.auth_cli_token_ttl_seconds == 15 * 60
    assert settings.auth_attempt_budget_capacity == 5
    assert settings.auth_max_challenge_attempts == 5
    assert settings.totp_master_key_bytes is None
    assert settings.enable_docs is False
    assert settings.trust_proxy is False


def test_admin_token_requires_at_least_32_utf8_bytes_without_echoing_value() -> None:
    raw = "too-short-admin-token"
    with pytest.raises(ValidationError) as captured:
        Settings(admin_token=raw)
    assert raw not in str(captured.value)


def test_totp_master_key_accepts_unpadded_base64url_for_exactly_32_bytes() -> None:
    raw = b"k" * 32
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    settings = Settings(admin_token=ADMIN_TOKEN, totp_master_key=encoded)
    assert settings.totp_master_key_bytes == raw
    assert encoded not in repr(settings)


@pytest.mark.parametrize(
    "encoded",
    [
        base64.urlsafe_b64encode(b"short").decode().rstrip("="),
        "not+base64url",
        base64.urlsafe_b64encode(b"x" * 32).decode(),
    ],
)
def test_totp_master_key_rejects_wrong_length_alphabet_or_padding_without_echo(
    encoded: str,
) -> None:
    with pytest.raises(ValidationError) as captured:
        Settings(admin_token=ADMIN_TOKEN, totp_master_key=encoded)
    assert encoded not in str(captured.value)


def test_totp_master_key_can_be_loaded_from_a_file(tmp_path) -> None:
    raw = b"f" * 32
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    key_file = tmp_path / "totp.key"
    key_file.write_text(encoded)
    settings = Settings(admin_token=ADMIN_TOKEN, totp_master_key_file=key_file)
    assert settings.totp_master_key_bytes == raw


def test_totp_master_key_sources_are_mutually_exclusive(tmp_path) -> None:
    encoded = base64.urlsafe_b64encode(b"f" * 32).decode().rstrip("=")
    key_file = tmp_path / "totp.key"
    key_file.write_text(encoded)
    with pytest.raises(ValidationError, match="only one"):
        Settings(
            admin_token=ADMIN_TOKEN,
            totp_master_key=encoded,
            totp_master_key_file=key_file,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com",
        "https://example.com/app",
        "https://example.com/?query=yes",
        "https://example.com/#fragment",
    ],
)
def test_public_base_url_is_a_canonical_root_http_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(admin_token=ADMIN_TOKEN, public_base_url=url)


def test_public_integration_environment_names_are_stable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TERMFLOW_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("TERMFLOW_STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("TERMFLOW_PUBLIC_BASE_URL", "https://termflow.example")
    monkeypatch.setenv(
        "TERMFLOW_TRUSTED_WEB_ORIGINS",
        "https://termflow.example,https://admin.termflow.example",
    )
    monkeypatch.setenv("TERMFLOW_BROWSER_SESSION_TTL_SECONDS", "1234")
    monkeypatch.setenv("TERMFLOW_ENROLLMENT_TOKEN_TTL_SECONDS", "45")
    monkeypatch.setenv("TERMFLOW_TERMINAL_MAX_FRAME_BYTES", "4096")
    monkeypatch.setenv("TERMFLOW_TERMINAL_INPUT_RATE_BYTES_PER_SECOND", "8192")
    monkeypatch.setenv("TERMFLOW_TERMINAL_QUEUE_MAX_MESSAGES", "32")
    monkeypatch.setenv("TERMFLOW_TERMINAL_QUEUE_MAX_BYTES", "262144")
    monkeypatch.setenv("TERMFLOW_TERMINAL_RESUME_GRACE_SECONDS", "45")

    settings = Settings(_env_file=None)

    assert settings.static_dir == tmp_path / "static"
    assert str(settings.public_base_url) == "https://termflow.example/"
    assert settings.allowed_web_origins == (
        "https://termflow.example",
        "https://admin.termflow.example",
    )
    assert settings.browser_session_ttl_seconds == 1234
    assert settings.enrollment_token_ttl_seconds == 45
    assert settings.terminal_max_frame_bytes == 4096
    assert settings.terminal_input_rate_bytes_per_second == 8192
    assert settings.terminal_queue_max_messages == 32
    assert settings.terminal_queue_max_bytes == 262144
    assert settings.terminal_resume_grace_seconds == 45
