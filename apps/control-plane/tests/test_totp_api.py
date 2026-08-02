import base64
import hashlib
import hmac
import struct
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"
ORIGIN = "http://127.0.0.1:8000"


def _code(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return f"{value % 1_000_000:06d}"


def _counter(offset: int = 0) -> int:
    return int(time.time()) // 30 + offset


@pytest.fixture
def totp_client(tmp_path, monkeypatch):
    counter = int(time.time()) // 30
    observed_at = datetime.fromtimestamp(counter * 30 + 15, tz=UTC)
    monkeypatch.setattr(time, "time", lambda: observed_at.timestamp())
    encoded_key = base64.urlsafe_b64encode(b"t" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'totp-api.db'}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
        auth_attempt_budget_capacity=100,
        auth_attempt_refill_seconds=1,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        client.app.state.authentication_service._clock = lambda: observed_at
        yield client


def _login(client: TestClient, token: str = ADMIN_TOKEN):
    return client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": token},
    )


def _begin_setup(client: TestClient):
    response = client.post(
        "/api/v1/admin/totp/setups",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    secret = base64.b32decode(body["setup_key"])
    assert len(secret) == 20
    assert f"secret={body['setup_key']}" in body["provisioning_uri"]
    assert "issuer=TermFlow" in body["provisioning_uri"]
    assert "algorithm=SHA1" in body["provisioning_uri"]
    assert "digits=6" in body["provisioning_uri"]
    assert "period=30" in body["provisioning_uri"]
    return body, secret


def _configure_totp(client: TestClient, *, confirm_offset: int = -1) -> bytes:
    assert _login(client).status_code == 201
    setup, secret = _begin_setup(client)
    confirmed = client.post(
        f"/api/v1/admin/totp/setups/{setup['setup_id']}/confirm",
        headers={"Origin": ORIGIN},
        json={"code": _code(secret, _counter(confirm_offset))},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {
        "configured": True,
        "enabled": False,
        "available": True,
    }
    return secret


def _enable_totp(client: TestClient, *, confirm_offset: int = -1) -> bytes:
    secret = _configure_totp(client, confirm_offset=confirm_offset)
    enabled = client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": ORIGIN},
        json={
            "admin_token": ADMIN_TOKEN,
            "code": _code(secret, _counter(confirm_offset + 1)),
        },
    )
    assert enabled.status_code == 200
    assert enabled.json() == {
        "configured": True,
        "enabled": True,
        "available": True,
    }
    return secret


def test_totp_status_accepts_browser_get_without_origin_and_setup_requires_exact_origin(
    totp_client,
) -> None:
    assert _login(totp_client).status_code == 201

    status = totp_client.get("/api/v1/admin/totp", headers={"Origin": ORIGIN})
    assert status.status_code == 200
    assert status.json() == {
        "configured": False,
        "enabled": False,
        "available": True,
    }
    browser_status = totp_client.get("/api/v1/admin/totp")
    assert browser_status.status_code == 200
    assert browser_status.json() == status.json()
    assert (
        totp_client.get(
            "/api/v1/admin/totp",
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )

    setup, _secret = _begin_setup(totp_client)
    assert set(setup) == {"setup_id", "provisioning_uri", "setup_key", "expires_at"}


def test_totp_setup_is_unavailable_without_independent_master_key(tmp_path) -> None:
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'no-key.db'}",
        allow_insecure_loopback=True,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        assert _login(client).status_code == 201
        status = client.get("/api/v1/admin/totp", headers={"Origin": ORIGIN})
        setup = client.post(
            "/api/v1/admin/totp/setups",
            headers={"Origin": ORIGIN},
            json={"admin_token": ADMIN_TOKEN},
        )

    assert status.json() == {
        "configured": False,
        "enabled": False,
        "available": False,
    }
    assert setup.status_code == 409
    assert setup.json()["error"]["code"] == "totp_unavailable"
    assert "setup_key" not in setup.text


def test_enabled_totp_fails_closed_after_restart_without_master_key(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'missing-key-restart.db'}"
    encoded_key = base64.urlsafe_b64encode(b"t" * 32).decode().rstrip("=")
    keyed = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=database_url,
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
        auth_attempt_budget_capacity=100,
    )
    keyed_app = create_app(settings=keyed, database=Database(database_url))
    with TestClient(keyed_app) as client:
        _enable_totp(client)

    missing_key = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=database_url,
        allow_insecure_loopback=True,
        auth_attempt_budget_capacity=100,
    )
    restarted_app = create_app(settings=missing_key, database=Database(database_url))
    with TestClient(restarted_app) as restarted:
        assert restarted.get("/healthz").status_code == 200
        login = _login(restarted)

    assert login.status_code == 401
    assert login.json()["error"]["code"] == "authentication_failed"
    assert "challenge_id" not in login.text
    assert "set-cookie" not in login.headers


def test_enabled_totp_changes_login_to_opaque_challenge_and_rejects_replay(
    totp_client,
) -> None:
    secret = _enable_totp(totp_client)
    assert totp_client.delete(
        "/api/v1/admin/session", headers={"Origin": ORIGIN}
    ).status_code == 200

    challenge = _login(totp_client)
    replay_challenge = _login(totp_client)
    wrong_token = _login(totp_client, token="wrong")

    assert wrong_token.status_code == 401
    assert wrong_token.json()["error"]["code"] == "authentication_failed"
    assert challenge.status_code == 202
    assert challenge.json()["status"] == "totp_required"
    assert set(challenge.json()) == {"status", "challenge_id", "expires_at"}
    assert "set-cookie" not in challenge.headers

    challenge_id = challenge.json()["challenge_id"]
    completed = totp_client.post(
        f"/api/v1/admin/sessions/{challenge_id}/totp",
        headers={"Origin": ORIGIN},
        json={"code": _code(secret, _counter(1))},
    )
    assert completed.status_code == 201
    assert "HttpOnly" in completed.headers["set-cookie"]
    assert totp_client.get("/api/v1/admin/session").status_code == 200

    replay = totp_client.post(
        f"/api/v1/admin/sessions/{replay_challenge.json()['challenge_id']}/totp",
        headers={"Origin": ORIGIN},
        json={"code": _code(secret, _counter(1))},
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == wrong_token.json()["error"]["code"]
    assert replay.json()["error"]["message"] == wrong_token.json()["error"]["message"]


def test_configured_but_disabled_totp_does_not_change_login(totp_client) -> None:
    _configure_totp(totp_client)
    assert totp_client.delete(
        "/api/v1/admin/session", headers={"Origin": ORIGIN}
    ).status_code == 200

    login = _login(totp_client)

    assert login.status_code == 201
    assert "challenge_id" not in login.text


def test_enable_requires_cookie_origin_primary_token_and_fresh_totp(totp_client) -> None:
    secret = _configure_totp(totp_client)
    payload = {"admin_token": ADMIN_TOKEN, "code": _code(secret, _counter())}

    wrong_origin = totp_client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": "https://evil.example"},
        json=payload,
    )
    saved_cookies = dict(totp_client.cookies)
    totp_client.cookies.clear()
    missing_cookie = totp_client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": ORIGIN},
        json=payload,
    )
    for name, value in saved_cookies.items():
        totp_client.cookies.set(name, value)
    wrong_token = totp_client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": ORIGIN},
        json={**payload, "admin_token": "wrong"},
    )
    totp_client.app.state.auth_rate_limiter.record_success("totp_enable", "testclient")
    replay = totp_client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": ORIGIN},
        json={**payload, "code": _code(secret, _counter(-1))},
    )
    totp_client.app.state.auth_rate_limiter.record_success("totp_enable", "testclient")
    enabled = totp_client.post(
        "/api/v1/admin/totp/enable",
        headers={"Origin": ORIGIN},
        json=payload,
    )

    assert wrong_origin.status_code == 403
    assert missing_cookie.status_code == 401
    assert wrong_token.status_code == 401
    assert replay.status_code == 401
    assert enabled.status_code == 200
    assert enabled.json() == {
        "configured": True,
        "enabled": True,
        "available": True,
    }


def test_disable_requires_cookie_admin_token_and_fresh_current_totp(totp_client) -> None:
    secret = _enable_totp(totp_client, confirm_offset=-1)

    wrong_origin = totp_client.request(
        "DELETE",
        "/api/v1/admin/totp",
        headers={"Origin": "https://evil.example"},
        json={"admin_token": ADMIN_TOKEN, "code": _code(secret, _counter(1))},
    )
    wrong_token = totp_client.request(
        "DELETE",
        "/api/v1/admin/totp",
        headers={"Origin": ORIGIN},
        json={"admin_token": "wrong", "code": _code(secret, _counter())},
    )
    totp_client.app.state.auth_rate_limiter.record_success("totp_disable", "testclient")
    disabled = totp_client.request(
        "DELETE",
        "/api/v1/admin/totp",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN, "code": _code(secret, _counter(1))},
    )

    assert wrong_origin.status_code == 403
    assert wrong_token.status_code == 401
    assert disabled.status_code == 200
    assert disabled.json() == {
        "configured": True,
        "enabled": False,
        "available": True,
    }
    status = totp_client.get("/api/v1/admin/totp", headers={"Origin": ORIGIN})
    assert status.json() == disabled.json()


def test_confirm_setup_is_single_use_and_expired_setup_is_rejected(totp_client) -> None:
    assert _login(totp_client).status_code == 201
    setup, secret = _begin_setup(totp_client)
    url = f"/api/v1/admin/totp/setups/{setup['setup_id']}/confirm"
    payload = {"code": _code(secret, _counter())}

    first = totp_client.post(url, headers={"Origin": ORIGIN}, json=payload)
    second = totp_client.post(url, headers={"Origin": ORIGIN}, json=payload)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "authentication_failed"


def test_reconfigure_requires_current_totp_and_replaces_the_old_secret(totp_client) -> None:
    old_secret = _enable_totp(totp_client, confirm_offset=-1)
    missing_current = totp_client.post(
        "/api/v1/admin/totp/setups",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert missing_current.status_code == 401
    totp_client.app.state.auth_rate_limiter.record_success("totp_setup", "testclient")

    replacement = totp_client.post(
        "/api/v1/admin/totp/setups",
        headers={"Origin": ORIGIN},
        json={
            "admin_token": ADMIN_TOKEN,
            "totp_code": _code(old_secret, _counter(1)),
        },
    )
    assert replacement.status_code == 201
    replacement_secret = base64.b32decode(replacement.json()["setup_key"])
    assert replacement_secret != old_secret
    confirmed = totp_client.post(
        f"/api/v1/admin/totp/setups/{replacement.json()['setup_id']}/confirm",
        headers={"Origin": ORIGIN},
        json={"code": _code(replacement_secret, _counter())},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {
        "configured": True,
        "enabled": True,
        "available": True,
    }

    old_authenticator = totp_client.post(
        "/api/v1/admin/totp/setups",
        headers={"Origin": ORIGIN},
        json={
            "admin_token": ADMIN_TOKEN,
            "totp_code": _code(old_secret, _counter(1)),
        },
    )
    assert old_authenticator.status_code == 401


def test_openapi_has_no_totp_reset_or_recovery_route(totp_client) -> None:
    paths = totp_client.get("/openapi.json").json()["paths"]
    login_responses = paths["/api/v1/admin/sessions"]["post"]["responses"]
    assert "201" in login_responses
    assert "202" in login_responses
    assert "/api/v1/admin/totp/reset" not in paths
    assert all("recover" not in path for path in paths)
