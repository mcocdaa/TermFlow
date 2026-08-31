import json
import re
from uuid import uuid4

import httpx
import pytest
from termflow_node import __version__
from termflow_node.cli import app
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import ControlPlaneClient, InsecureServerUrl
from termflow_protocol import InstallationEnrollResponse, InstanceListResponse
from typer.testing import CliRunner


@pytest.mark.asyncio
async def test_enrollment_client_posts_computer_identity_and_validates_response(
    monkeypatch,
) -> None:
    installation_id = uuid4()
    monkeypatch.setattr("termflow_node.control_plane_client.socket.gethostname", lambda: "devbox")
    monkeypatch.setattr("termflow_node.control_plane_client.platform.system", lambda: "TestOS")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://termflow.example.com/api/v1/installations/enroll"
        assert json.loads(request.content) == {
            "enrollment_token": "one-time-secret",
            "hostname": "devbox",
            "platform": "TestOS",
            "client_version": __version__,
        }
        return httpx.Response(
            200,
            json={
                "installation_id": str(installation_id),
                "installation_token": "installation-secret-token-that-is-long-enough",
            },
        )

    client = ControlPlaneClient(transport=httpx.MockTransport(handler))
    response = await client.enroll("https://termflow.example.com", "one-time-secret")
    assert response.installation_id == installation_id


@pytest.mark.asyncio
async def test_enrollment_client_rejects_public_plain_http() -> None:
    client = ControlPlaneClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(InsecureServerUrl):
        await client.enroll("http://termflow.example.com", "secret")


@pytest.mark.asyncio
async def test_enrollment_client_allows_plain_http_when_permitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://192.168.0.53:8765/api/v1/installations/enroll"
        return httpx.Response(
            200,
            json={
                "installation_id": str(uuid4()),
                "installation_token": "installation-secret-token-that-is-long-enough",
            },
        )

    client = ControlPlaneClient(transport=httpx.MockTransport(handler))
    response = await client.enroll(
        "http://192.168.0.53:8765",
        "secret",
        allow_insecure_http=True,
    )
    assert response.installation_token == "installation-secret-token-that-is-long-enough"

    client = ControlPlaneClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(InsecureServerUrl):
        await client.enroll("http://192.168.0.53:8765", "secret")


@pytest.mark.asyncio
async def test_instance_sync_client_uses_its_installation_token() -> None:
    installation = InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id=uuid4(),
        installation_token="installation-secret-token-that-is-long-enough",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://termflow.example.com/api/v1/instances/mine"
        assert request.headers["Authorization"] == (
            "Bearer installation-secret-token-that-is-long-enough"
        )
        return httpx.Response(200, json={"instances": []})

    client = ControlPlaneClient(transport=httpx.MockTransport(handler))

    response = await client.list_owned_instances(installation)

    assert response == InstanceListResponse(instances=[])


@pytest.mark.asyncio
async def test_health_client_reports_the_control_plane_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://termflow.example.com/healthz"
        return httpx.Response(200, json={"status": "ok"})

    client = ControlPlaneClient(transport=httpx.MockTransport(handler))

    ok, detail = await client.probe_health("https://termflow.example.com")

    assert ok is True
    assert detail == "reachable"


@pytest.mark.asyncio
async def test_installation_probe_reports_revoked_only_for_auth_failures() -> None:
    installation = InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id=uuid4(),
        installation_token="installation-secret-token-that-is-long-enough",
    )

    def revoked(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/instances/mine"
        return httpx.Response(401)

    assert await ControlPlaneClient(transport=httpx.MockTransport(revoked)).installation_revoked(
        installation
    )

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        await ControlPlaneClient(transport=httpx.MockTransport(unavailable)).installation_revoked(
            installation
        )


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


def test_login_probe_error_keeps_force_requirement(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.json")
    existing_id = uuid4()
    store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=existing_id,
            installation_token="existing-installation-secret-token-that-is-long-enough",
        )
    )

    async def broken_probe(self, installation) -> bool:
        raise httpx.ConnectError("no route to host")

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        raise AssertionError("enrollment must not run when the probe cannot verify")

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "installation_revoked", broken_probe)
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


def test_login_never_probes_a_different_server(tmp_path, monkeypatch) -> None:
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
        raise AssertionError("the probe must not run against a different server")

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        raise AssertionError("enrollment must not run without --force")

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "installation_revoked", fake_probe)
    monkeypatch.setattr(ControlPlaneClient, "enroll", fake_enroll)

    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "https://other.example.com",
            "--enrollment-token",
            "one-time-secret",
        ],
        env={"GITHUB_ACTIONS": "true"},
    )

    assert result.exit_code != 0
    plain_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)
    assert "--force" in plain_output
    assert store.load().installation_id == existing_id


def test_login_saves_private_config_without_printing_tokens(tmp_path, monkeypatch, caplog) -> None:
    store = ConfigStore(tmp_path / "config.json")
    installation_id = uuid4()

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        return InstallationEnrollResponse(
            installation_id=installation_id,
            installation_token="installation-secret-token-that-is-long-enough",
        )

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
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
    assert str(installation_id) in result.stdout
    combined_output = result.stdout + result.stderr + caplog.text
    assert "one-time-secret" not in combined_output
    assert "installation-secret-token" not in combined_output
    assert store.load().installation_id == installation_id

    refused = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "https://termflow.example.com",
            "--enrollment-token",
            "another-secret",
        ],
        env={"GITHUB_ACTIONS": "true"},
    )
    assert refused.exit_code != 0
    plain_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", refused.output)
    assert "--force" in plain_output


def test_login_accepts_public_registration_code_flag(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.json")

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        assert enrollment_token == "single-use-code"
        return InstallationEnrollResponse(
            installation_id=uuid4(),
            installation_token="installation-secret-token-that-is-long-enough",
        )

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "enroll", fake_enroll)
    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "https://termflow.example.com",
            "--code",
            "single-use-code",
        ],
    )

    assert result.exit_code == 0, result.output
    assert store.exists()


def test_login_persists_allow_insecure_http_flag(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.json")

    async def fake_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        assert allow_insecure_http is True
        assert server_url == "http://192.168.0.53:8765"
        return InstallationEnrollResponse(
            installation_id=uuid4(),
            installation_token="installation-secret-token-that-is-long-enough",
        )

    monkeypatch.setattr(ConfigStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(ControlPlaneClient, "enroll", fake_enroll)
    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "http://192.168.0.53:8765",
            "--code",
            "single-use-code",
            "--allow-insecure-http",
        ],
    )

    assert result.exit_code == 0, result.output
    config = store.load()
    assert config.allow_insecure_http is True
    assert str(config.server_url) == "http://192.168.0.53:8765/"

    store.path.unlink()

    async def unexpected_enroll(
        self, server_url: str, enrollment_token: str, allow_insecure_http: bool = False
    ):
        raise AssertionError("enrollment must not run without --allow-insecure-http")

    monkeypatch.setattr(ControlPlaneClient, "enroll", unexpected_enroll)
    refused = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "http://192.168.0.53:8765",
            "--code",
            "single-use-code",
        ],
        env={"GITHUB_ACTIONS": "true"},
    )
    assert refused.exit_code != 0
    assert not store.exists()
