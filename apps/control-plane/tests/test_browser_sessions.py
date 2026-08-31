import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import _authentication_epoch_loop, create_app
from termflow_control_plane.auth.sessions import BrowserSessionStore
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"
DEV_ORIGIN = "http://127.0.0.1:8000"


def _login(client: TestClient, token: str = ADMIN_TOKEN, origin: str = DEV_ORIGIN):
    return client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": origin},
        json={"admin_token": token},
    )


def test_browser_login_status_logout_and_cookie_admin_access(client) -> None:
    logged_in = _login(client)
    assert logged_in.status_code == 201
    assert logged_in.headers["cache-control"] == "no-store"
    cookie = logged_in.headers["set-cookie"]
    assert "termflow_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie

    status = client.get("/api/v1/admin/session")
    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    enrollment = client.post(
        "/api/v1/enrollment-tokens",
        headers={"Origin": DEV_ORIGIN},
    )
    assert enrollment.status_code == 201

    logged_out = client.delete("/api/v1/admin/session", headers={"Origin": DEV_ORIGIN})
    assert logged_out.status_code == 200
    assert logged_out.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/admin/session").status_code == 401


def test_admin_session_routes_are_the_only_canonical_openapi_paths(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/admin/sessions" in paths
    assert set(paths["/api/v1/admin/sessions"]) == {"post"}
    assert "/api/v1/admin/session" in paths
    assert set(paths["/api/v1/admin/session"]) == {"get", "delete"}
    assert "/api/v1/session" not in paths


def test_browser_login_rejects_invalid_token_and_origin(client) -> None:
    wrong_token = _login(client, token="wrong")
    wrong_origin = _login(client, origin="https://evil.example")
    missing_origin = client.post(
        "/api/v1/admin/sessions",
        json={"admin_token": ADMIN_TOKEN},
    )
    assert wrong_token.status_code == 401
    assert wrong_origin.status_code == 403
    assert missing_origin.status_code == 403
    assert "set-cookie" not in wrong_token.headers


def test_bearer_authentication_remains_available(client, admin_headers) -> None:
    response = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
    assert response.status_code == 201


def test_session_store_hashes_secrets_expires_and_enforces_capacity() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    clock_value = [now]
    revoked: list[str] = []
    store = BrowserSessionStore(
        ttl=timedelta(seconds=10),
        capacity=2,
        clock=lambda: clock_value[0],
        on_revoke=revoked.append,
    )
    first, _ = store.create()
    second, second_expiry = store.create()
    third, _ = store.create()

    assert store.authenticate(first) is None
    assert len(revoked) == 1
    assert store.authenticate(second) == second_expiry
    assert store.authenticate(third) is not None
    representation = repr(store)
    assert first not in representation
    assert second not in representation
    assert third not in representation

    clock_value[0] = now + timedelta(seconds=11)
    assert store.authenticate(second) is None
    assert store.live_count == 0
    assert len(revoked) == 3


def test_session_store_revokes_sessions_from_an_old_auth_epoch() -> None:
    store = BrowserSessionStore(ttl=timedelta(minutes=5), capacity=2)
    secret, _ = store.create(epoch=7)

    assert store.authenticate(secret, epoch=7) is not None
    assert store.authenticate(secret, epoch=8) is None
    assert store.live_count == 0


def test_http_cookie_is_rejected_when_persisted_auth_epoch_changes(client) -> None:
    assert _login(client).status_code == 201
    assert client.app.state.browser_sessions.live_count == 1

    client.portal.call(client.app.state.repositories.auth_state.reset_and_increment_epoch)

    assert client.get("/api/v1/admin/session").status_code == 401
    assert client.app.state.browser_sessions.live_count == 0


@pytest.mark.asyncio
async def test_epoch_watcher_syncs_hubs_when_cookie_request_advanced_store_first(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'epoch-race.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    sessions = BrowserSessionStore(ttl=timedelta(minutes=5), capacity=2)
    terminal_hub = TerminalHub(queue_max_messages=2, queue_max_bytes=1024)
    event_hub = EventHub(queue_size=2)
    terminal = await terminal_hub.register(uuid4(), session_key=None, auth_epoch=1)
    subscriber = await event_hub.subscribe(instance_id=None, auth_epoch=1)
    stop = asyncio.Event()
    watcher: asyncio.Task[None] | None = None
    try:
        assert await repositories.auth_state.reset_and_increment_epoch() == 2
        sessions.synchronize_epoch(2)
        watcher = asyncio.create_task(
            _authentication_epoch_loop(
                repositories,
                sessions,
                terminal_hub,
                event_hub,
                stop,
            )
        )
        await asyncio.sleep(1.1)

        assert terminal.terminated
        assert subscriber.closed.is_set()
        replacement = await terminal_hub.register(
            uuid4(),
            session_key=None,
            auth_epoch=2,
        )
        assert replacement.auth_epoch == 2
        assert not (
            await event_hub.subscribe(instance_id=None, auth_epoch=2)
        ).closed.is_set()
    finally:
        stop.set()
        if watcher is not None:
            await watcher
        await database.dispose()


def test_https_uses_secure_host_cookie(tmp_path) -> None:
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'secure.db'}",
        public_base_url="https://termflow.example",
        trusted_web_origins=("https://termflow.example",),
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app, base_url="https://termflow.example") as client:
        response = _login(client, origin="https://termflow.example")
        cookie = response.headers["set-cookie"]
        assert "__Host-termflow_session=" in cookie
        assert "Secure" in cookie
        assert "Domain=" not in cookie
        assert client.get("/api/v1/admin/session").status_code == 200


def test_cookie_state_change_rejects_untrusted_origin(client) -> None:
    assert _login(client).status_code == 201
    response = client.post(
        "/api/v1/enrollment-tokens",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
