import json
from uuid import uuid4

import httpx
import pytest
from termflow_node.cli import app
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import ControlPlaneClient, InsecureServerUrl
from termflow_protocol import InstallationEnrollResponse
from typer.testing import CliRunner


@pytest.mark.asyncio
async def test_enrollment_client_posts_and_validates_response() -> None:
    installation_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://termflow.example.com/api/v1/installations/enroll"
        assert json.loads(request.content) == {"enrollment_token": "one-time-secret"}
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


def test_login_saves_private_config_without_printing_tokens(tmp_path, monkeypatch, caplog) -> None:
    store = ConfigStore(tmp_path / "config.json")
    installation_id = uuid4()

    async def fake_enroll(self, server_url: str, enrollment_token: str):
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
    )
    assert refused.exit_code != 0
    assert "--force" in refused.output
