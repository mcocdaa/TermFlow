import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore, InsecureConfigError


def test_config_is_atomic_private_and_round_trips(tmp_path) -> None:
    store = ConfigStore(tmp_path / "private" / "config.json")
    expected = InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id=uuid4(),
        installation_token="secret-token",
    )
    store.save(expected)
    assert store.load() == expected
    assert json.loads(store.path.read_text()) == {
        "server_url": "https://termflow.example.com/",
        "installation_id": str(expected.installation_id),
        "installation_token": "secret-token",
    }
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(store.path.parent.glob("*.tmp"))
    assert b"secret-token" in store.path.read_bytes()
    assert "secret-token" not in repr(expected)


def test_load_rejects_group_or_other_permissions(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token="secret-token",
        )
    )
    store.path.chmod(0o640)
    with pytest.raises(InsecureConfigError):
        store.load()


def test_load_migrates_legacy_false_insecure_http_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "https://termflow.example",
                "installation_id": str(uuid4()),
                "installation_token": "secret",
                "allow_insecure_http": False,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    loaded = ConfigStore(path).load()

    assert not hasattr(loaded, "allow_insecure_http")


def test_load_rejects_legacy_true_insecure_http_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "http://192.0.2.10:8080",
                "installation_id": str(uuid4()),
                "installation_token": "secret",
                "allow_insecure_http": True,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(
        InsecureConfigError,
        match="no longer supports public HTTP; run `termflow login` with an HTTPS URL",
    ):
        ConfigStore(path).load()
