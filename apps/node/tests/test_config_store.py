import json
import stat
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


def test_config_round_trips_allow_insecure_http(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    expected = InstallationConfig(
        server_url="http://192.168.0.53:8765",
        installation_id=uuid4(),
        installation_token="secret-token",
        allow_insecure_http=True,
    )
    store.save(expected)
    assert store.load() == expected


def test_config_without_flag_field_defaults_to_false(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.path.write_text(
        json.dumps(
            {
                "server_url": "http://192.168.0.53:8765",
                "installation_id": str(uuid4()),
                "installation_token": "secret-token",
            }
        )
    )
    store.path.chmod(0o600)
    assert store.load().allow_insecure_http is False
