import pytest
from pydantic import ValidationError
from termflow_control_plane.config import Settings


def test_offline_timeout_must_exceed_heartbeat() -> None:
    with pytest.raises(ValidationError):
        Settings(
            admin_token="admin-secret",
            heartbeat_interval_seconds=15,
            offline_after_seconds=15,
        )


def test_control_plane_defaults_are_single_process_friendly() -> None:
    settings = Settings(admin_token="admin-secret")
    assert settings.connection_queue_size == 256
    assert settings.event_queue_size == 512
    assert settings.max_input_bytes == 16 * 1024
    assert settings.browser_session_ttl_seconds == 8 * 60 * 60
    assert settings.terminal_max_frame_bytes == 65_536
    assert settings.allowed_web_origins == ("http://127.0.0.1:8000",)


def test_public_integration_environment_names_are_stable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TERMFLOW_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("TERMFLOW_STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("TERMFLOW_PUBLIC_BASE_URL", "https://termflow.example/app")
    monkeypatch.setenv(
        "TERMFLOW_TRUSTED_WEB_ORIGINS",
        "https://termflow.example,https://admin.termflow.example",
    )
    monkeypatch.setenv("TERMFLOW_BROWSER_SESSION_TTL_SECONDS", "1234")
    monkeypatch.setenv("TERMFLOW_TERMINAL_MAX_FRAME_BYTES", "4096")

    settings = Settings(_env_file=None)

    assert settings.static_dir == tmp_path / "static"
    assert str(settings.public_base_url) == "https://termflow.example/app"
    assert settings.allowed_web_origins == (
        "https://termflow.example",
        "https://admin.termflow.example",
    )
    assert settings.browser_session_ttl_seconds == 1234
    assert settings.terminal_max_frame_bytes == 4096
