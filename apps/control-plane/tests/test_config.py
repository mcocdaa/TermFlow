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
