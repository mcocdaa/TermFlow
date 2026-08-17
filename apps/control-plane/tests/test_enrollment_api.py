from datetime import UTC, datetime

from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


def test_enrollment_lifecycle(tmp_path, admin_headers) -> None:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'relay-url.db'}",
        public_base_url="https://relay.example.com",
        allow_insecure_loopback=True,
        enable_docs=True,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}

        issued_after = datetime.now(UTC)
        issued = client.post(
            "/api/v1/enrollment-tokens",
            headers=admin_headers,
            json={"display_name": "跑步工作站"},
        )
        assert issued.status_code == 201
        body = issued.json()
        assert body["server_url"] == "https://relay.example.com"
        assert body["login_command"] == (
            f"termflow login --server https://relay.example.com --code {body['token']}"
        )
        raw = body["token"]
        assert len(raw) >= 43
        expires_at = datetime.fromisoformat(issued.json()["expires_at"])
        assert 59 <= (expires_at - issued_after).total_seconds() <= 61

        enrolled = client.post(
            "/api/v1/installations/enroll",
            json={
                "enrollment_token": raw,
                "hostname": "devbox",
                "platform": "Linux",
                "client_version": "0.1.0",
            },
        )
        assert enrolled.status_code == 201
        assert len(enrolled.json()["installation_token"]) >= 43

        replay = client.post(
            "/api/v1/installations/enroll",
            json={"enrollment_token": raw},
        )
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "invalid_enrollment_token"

        installation_token = enrolled.json()["installation_token"]
        installation = client.portal.call(
            client.app.state.repositories.installations.get_by_token_hash,
            hash_token(installation_token),
        )
        assert installation.hostname == "devbox"
        assert installation.display_name == "跑步工作站"
        assert installation.platform == "Linux"
        assert installation.client_version == "0.1.0"

        legacy = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
        assert legacy.status_code == 201
        legacy_enrolled = client.post(
            "/api/v1/installations/enroll",
            json={"enrollment_token": legacy.json()["token"], "hostname": "legacy-host"},
        )
        assert legacy_enrolled.status_code == 201
        legacy_installation = client.portal.call(
            client.app.state.repositories.installations.get_by_token_hash,
            hash_token(legacy_enrolled.json()["installation_token"]),
        )
        assert legacy_installation.display_name == "legacy-host"


def test_admin_rejection_and_no_raw_tokens_in_logs(client, admin_headers, caplog) -> None:
    missing = client.post("/api/v1/enrollment-tokens")
    wrong = client.post(
        "/api/v1/enrollment-tokens",
        headers={"Authorization": "Bearer wrong"},
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"]["request_id"]

    issued = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
    token = issued.json()["token"]
    client.post("/api/v1/installations/enroll", json={"enrollment_token": token})
    assert token not in caplog.text
