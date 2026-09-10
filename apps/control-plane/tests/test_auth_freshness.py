"""RED tests for the v0.2.0 fresh administrator authentication contract.

These tests intentionally exercise the public authentication boundaries rather
than inspecting implementation-only flags.  They are written before the
Task-5 implementation and therefore are expected to fail against the current
checkout.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from termflow_control_plane.api import dependencies as auth_dependencies
from termflow_control_plane.auth.sessions import BrowserSessionStore
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"
ORIGIN = "http://127.0.0.1:8000"


def _request(
    *,
    app: object,
    method: str = "GET",
    path: str = "/api/v1/agent/admin",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("testclient", 1234),
        "server": ("127.0.0.1", 8000),
        "app": app,
    }
    return Request(scope)


def _native_jwk() -> dict[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": encode(numbers.x.to_bytes(32, "big")),
        "y": encode(numbers.y.to_bytes(32, "big")),
    }


def _jkt(jwk: dict[str, str]) -> str:
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_browser_session_retains_original_authenticated_at() -> None:
    first_authentication = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    now = first_authentication
    store = BrowserSessionStore(
        ttl=timedelta(hours=1),
        capacity=4,
        clock=lambda: now,
    )

    secret, expires_at = store.create(epoch=1, authenticated_at=first_authentication)
    assert store.authenticate(secret, epoch=1) == expires_at

    # Reading a cookie must not slide the strong-authentication timestamp.
    now = first_authentication + timedelta(seconds=120)
    record = store.get_record(secret, epoch=1)
    assert record is not None
    assert record.authenticated_at == first_authentication
    assert store.get_record(secret, epoch=1) == record


@pytest.mark.asyncio
async def test_refresh_rotation_preserves_original_authenticated_at(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'auth-freshness.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    first_authentication = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    try:
        parent = await repositories.auth_tokens.issue(
            "refresh-parent",
            kind="refresh",
            scopes=("terminal.read",),
            key_thumbprint=None,
            expires_at=first_authentication + timedelta(days=1),
            epoch=1,
            authenticated_at=first_authentication,
        )
        replacement = await repositories.auth_tokens.rotate_refresh(
            "refresh-parent",
            "refresh-replacement",
            expires_at=first_authentication + timedelta(days=2),
            epoch=1,
            now=first_authentication + timedelta(minutes=1),
        )
        assert replacement is not None
        assert parent.authenticated_at == first_authentication
        assert replacement.authenticated_at == first_authentication
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_raw_root_bearer_is_admin_but_not_fresh_admin(client) -> None:
    request = _request(app=client.app, path="/api/v1/agent/admin")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=ADMIN_TOKEN)
    settings: Settings = client.app.state.settings
    repositories: RepositoryBundle = client.app.state.repositories
    response = Response()

    context = await auth_dependencies.require_admin(
        request,
        response,
        credentials,
        settings,
        client.app.state.browser_sessions,
        repositories,
    )
    assert context.credential_kind == "root"
    assert context.authenticated_at is None

    with pytest.raises(TermFlowError) as exc_info:
        await auth_dependencies.require_fresh_admin(
            request,
            response,
            credentials,
            settings,
            client.app.state.browser_sessions,
            repositories,
        )
    assert exc_info.value.status_code == 428
    assert exc_info.value.code == "sensitive_action_reauthentication_required"


@pytest.mark.asyncio
async def test_expired_freshness_returns_428_stable_code(client) -> None:
    settings: Settings = client.app.state.settings
    repositories: RepositoryBundle = client.app.state.repositories
    first_authentication = datetime.now(UTC) - timedelta(seconds=301)
    secret, _ = client.app.state.browser_sessions.create(
        epoch=1,
        authenticated_at=first_authentication,
    )
    request = _request(
        app=client.app,
        path="/api/v1/agent/admin",
        headers={"cookie": f"termflow_session={secret}", "origin": ORIGIN},
    )
    with pytest.raises(TermFlowError) as exc_info:
        await auth_dependencies.require_fresh_admin(
            request,
            Response(),
            None,
            settings,
            client.app.state.browser_sessions,
            repositories,
        )
    assert exc_info.value.status_code == 428
    assert exc_info.value.code == "sensitive_action_reauthentication_required"
    assert ADMIN_TOKEN not in str(exc_info.value)
    assert str(first_authentication) not in str(exc_info.value)


def test_prompt_login_cannot_reuse_existing_browser_session(client) -> None:
    login = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert login.status_code == 201, login.text

    jwk = _native_jwk()
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
        b"="
    ).decode()
    response = client.get(
        "/api/v1/oauth/authorize",
        params={
            "response_type": "code",
            "prompt": "login",
            "client_name": "Fresh auth test client",
            "platform": "test",
            "redirect_uri": "termflow://auth/callback",
            "state": "fresh-auth-state-123456",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "dpop_jkt": _jkt(jwk),
            "public_jwk": json.dumps(jwk, separators=(",", ":")),
            "scopes": "terminal.read",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307, response.text
    transaction_id = parse_qs(urlsplit(response.headers["location"]).query)["transaction_id"][0]

    cookie_only = client.post(
        "/api/v1/oauth/authorize",
        json={"transaction_id": transaction_id, "decision": "allow"},
    )
    assert cookie_only.status_code == 401
    assert cookie_only.json()["error"]["code"] == "authentication_failed"


def test_authorization_code_deny_accepts_existing_browser_session(client) -> None:
    login = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert login.status_code == 201, login.text

    jwk = _native_jwk()
    verifier = "d" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
        b"="
    ).decode()
    response = client.get(
        "/api/v1/oauth/authorize",
        params={
            "response_type": "code",
            "client_name": "Deny test client",
            "platform": "test",
            "redirect_uri": "termflow://auth/callback",
            "state": "deny-auth-state-123456",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "dpop_jkt": _jkt(jwk),
            "public_jwk": json.dumps(jwk, separators=(",", ":")),
            "scopes": "terminal.read",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307, response.text
    transaction_id = parse_qs(urlsplit(response.headers["location"]).query)["transaction_id"][0]
    denied = client.post(
        "/api/v1/oauth/authorize",
        headers={"Origin": ORIGIN},
        json={"transaction_id": transaction_id, "decision": "deny"},
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["status"] == "denied"
