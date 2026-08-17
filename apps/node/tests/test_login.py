import re
from uuid import uuid4

from termflow_node.cli import app
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import ControlPlaneClient
from termflow_protocol import InstallationEnrollResponse
from typer.testing import CliRunner


def test_login_replaces_existing_config_after_revocation_confirmed(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.json")
    old_installation_id = uuid4()
    replacement_id = uuid4()
    store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=old_installation_id,
            installation_token="old-installation-secret-token-that-is-long-enough",
        )
    )

    async def fake_probe(self, installation) -> bool:
        return True

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        return InstallationEnrollResponse(
            installation_id=replacement_id,
            installation_token="replacement-installation-secret-token-that-is-long-enough",
        )

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "installation_revoked", fake_probe)
    monkeypatch.setattr(ControlPlaneClient, "enroll", fake_enroll)

    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "https://termflow.example.com",
            "--enrollment-token",
            "one-time-secret",
        ],
    )

    assert result.exit_code == 0, result.output
    config = store.load()
    assert config.installation_id == replacement_id
    assert config.installation_id != old_installation_id


def test_login_keeps_existing_config_when_old_installation_is_still_active(
    tmp_path, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "config.json")
    existing_id = uuid4()
    store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=existing_id,
            installation_token="existing-installation-secret-token-that-is-long-enough",
        )
    )

    async def fake_probe(self, installation) -> bool:
        return False

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        raise AssertionError("enrollment must not run for an active installation")

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "installation_revoked", fake_probe)
    monkeypatch.setattr(ControlPlaneClient, "enroll", fake_enroll)

    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "https://termflow.example.com",
            "--enrollment-token",
            "one-time-secret",
        ],
        env={"GITHUB_ACTIONS": "true"},
    )

    assert result.exit_code != 0
    plain_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)
    assert "--force" in plain_output
    assert store.load().installation_id == existing_id
