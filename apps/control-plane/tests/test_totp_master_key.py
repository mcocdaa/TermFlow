import base64
import concurrent.futures
import stat

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.master_key import resolve_totp_master_key
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_explicit_master_key_takes_priority_over_auto_file(tmp_path) -> None:
    raw = b"e" * 32
    auto_file = tmp_path / "auto-key"
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        totp_master_key=_encoded(raw),
        totp_auto_master_key_file=auto_file,
    )

    assert resolve_totp_master_key(settings) == raw
    assert not auto_file.exists()


def test_explicit_secret_file_takes_priority_over_auto_file(tmp_path) -> None:
    raw = b"f" * 32
    explicit_file = tmp_path / "docker-secret"
    explicit_file.write_text(_encoded(raw))
    auto_file = tmp_path / "auto-key"
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        totp_master_key_file=explicit_file,
        totp_auto_master_key_file=auto_file,
    )

    assert resolve_totp_master_key(settings) == raw
    assert not auto_file.exists()


def test_auto_master_key_is_private_persistent_and_concurrency_safe(tmp_path) -> None:
    auto_file = tmp_path / "data" / "totp-master-key"
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        totp_auto_master_key_file=auto_file,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        resolved = list(executor.map(lambda _: resolve_totp_master_key(settings), range(16)))

    assert resolved[0] is not None
    assert resolved == [resolved[0]] * 16
    encoded = auto_file.read_text()
    assert len(encoded) == 43
    assert "=" not in encoded
    assert base64.urlsafe_b64decode(encoded + "=") == resolved[0]
    assert stat.S_IMODE(auto_file.stat().st_mode) == 0o600
    assert resolve_totp_master_key(settings) == resolved[0]
    assert encoded not in repr(settings)


def test_auto_master_key_rejects_existing_group_or_other_access(tmp_path) -> None:
    auto_file = tmp_path / "totp-master-key"
    auto_file.write_text(_encoded(b"g" * 32))
    auto_file.chmod(0o640)
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        totp_auto_master_key_file=auto_file,
    )

    with pytest.raises(ValueError, match="private") as caught:
        resolve_totp_master_key(settings)

    assert auto_file.read_text() not in str(caught.value)


def test_app_lifespan_uses_the_auto_master_key(tmp_path) -> None:
    auto_file = tmp_path / "data" / "totp-master-key"
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control-plane.db'}",
        allow_insecure_loopback=True,
        totp_auto_master_key_file=auto_file,
    )

    with TestClient(
        create_app(settings=settings, database=Database(settings.database_url))
    ) as client:
        login = client.post(
            "/api/v1/admin/sessions",
            headers={"Origin": "http://127.0.0.1:8000"},
            json={"admin_token": ADMIN_TOKEN},
        )
        status = client.get(
            "/api/v1/admin/totp",
            headers={"Origin": "http://127.0.0.1:8000"},
        )

    assert login.status_code == 201
    assert status.json() == {
        "configured": False,
        "enabled": False,
        "available": True,
    }
    assert auto_file.exists()
