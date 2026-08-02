from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import struct
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.app import create_app
from termflow_control_plane.cli import app as cli_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    TerminalOpenedPayload,
    WireMessage,
)
from typer.testing import CliRunner

from .oauth_helpers import (
    approve_authorization,
    begin_authorization,
    exchange_authorization,
    key_and_jwk,
    proof,
)

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"
ORIGIN = "http://127.0.0.1:8000"


def _totp(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return f"{value % 1_000_000:06d}"


@pytest.fixture
def cli_token_client(tmp_path: Path) -> Iterator[TestClient]:
    key = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli-token.db'}",
        allow_insecure_loopback=True,
        totp_master_key=key,
        auth_cli_token_ttl_seconds=120,
        auth_attempt_budget_capacity=100,
        auth_attempt_refill_seconds=1,
    )
    with TestClient(
        create_app(settings=settings, database=Database(settings.database_url))
    ) as client:
        yield client


def _enable_totp(client: TestClient) -> bytes:
    login = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert login.status_code == 201
    setup = client.post(
        "/api/v1/admin/totp/setups",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert setup.status_code == 201
    secret = base64.b32decode(setup.json()["setup_key"])
    confirmed = client.post(
        f"/api/v1/admin/totp/setups/{setup.json()['setup_id']}/confirm",
        headers={"Origin": ORIGIN},
        json={"code": _totp(secret, int(time.time()) // 30 - 1)},
    )
    assert confirmed.status_code == 200
    return secret


def _provision_instance(client: TestClient) -> tuple[UUID, str]:
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    enrollment = client.post(
        "/api/v1/enrollment-tokens",
        headers=admin_headers,
    ).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment},
    ).json()
    instance_id = uuid4()
    registration = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "epoch-reset"},
    ).json()
    return instance_id, registration["instance_token"]


def _bridge_message(
    instance_id: UUID,
    message_type: MessageType,
    payload: BaseModel,
) -> str:
    return WireMessage(
        type=message_type,
        instance_id=instance_id,
        payload=payload.model_dump(mode="json"),
    ).model_dump_json()


def _open_terminal(
    stack: ExitStack,
    client: TestClient,
    instance_id: UUID,
    instance_token: str,
    headers: dict[str, str],
):
    bridge = stack.enter_context(
        client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {instance_token}"},
        )
    )
    bridge.send_text(
        _bridge_message(
            instance_id,
            MessageType.BRIDGE_HELLO,
            BridgeHelloPayload(name="epoch-reset"),
        )
    )
    terminal_path = f"/api/v1/terms/{instance_id}/terminal"
    terminal = stack.enter_context(client.websocket_connect(terminal_path, headers=headers))
    opened = WireMessage.model_validate(bridge.receive_json())
    assert opened.type is MessageType.TERMINAL_OPEN
    terminal_id = UUID(str(opened.payload["terminal_id"]))
    bridge.send_text(
        _bridge_message(
            instance_id,
            MessageType.TERMINAL_OPENED,
            TerminalOpenedPayload(
                terminal_id=terminal_id,
                stream_id=uuid4(),
                rows=24,
                cols=80,
            ),
        )
    )
    assert terminal.receive_json()["type"] == "terminal.ready"
    return terminal


def test_cli_token_is_short_lived_digest_only_and_scope_enforced(
    cli_token_client: TestClient,
) -> None:
    response = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={
            "admin_token": ADMIN_TOKEN,
            "scopes": ["computers.read"],
        },
    )

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 120
    assert body["scopes"] == ["computers.read"]
    raw_token = body["access_token"]
    assert len(raw_token) >= 32
    headers = {"Authorization": f"Bearer {raw_token}"}
    assert cli_token_client.get("/api/v1/dashboard", headers=headers).status_code == 200
    denied = cli_token_client.post("/api/v1/enrollment-tokens", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"

    database_path = Path(
        cli_token_client.app.state.settings.database_url.removeprefix(
            "sqlite+aiosqlite:///"
        )
    )
    with sqlite3.connect(database_path) as connection:
        digest, kind, scopes, epoch = connection.execute(
            "SELECT token_digest, kind, scopes, epoch FROM auth_tokens"
        ).fetchone()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert digest == hashlib.sha256(raw_token.encode()).hexdigest()
    assert (kind, scopes, epoch) == ("cli", '["computers.read"]', 1)
    assert raw_token.encode() not in database_path.read_bytes()


def test_cli_token_requires_fresh_replay_protected_totp_when_enabled(
    cli_token_client: TestClient,
) -> None:
    secret = _enable_totp(cli_token_client)
    current_code = _totp(secret, int(time.time()) // 30)
    missing = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={"admin_token": ADMIN_TOKEN},
    )
    cli_token_client.app.state.auth_rate_limiter.record_success("cli_token", "testclient")
    wrong_admin = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={"admin_token": "wrong-token", "totp_code": current_code},
    )
    cli_token_client.app.state.auth_rate_limiter.record_success("cli_token", "testclient")
    issued = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={"admin_token": ADMIN_TOKEN, "totp_code": current_code},
    )
    replay = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={"admin_token": ADMIN_TOKEN, "totp_code": current_code},
    )

    assert missing.status_code == 401
    assert wrong_admin.status_code == 401
    assert missing.json()["error"]["code"] == "authentication_failed"
    assert wrong_admin.json()["error"]["code"] == "authentication_failed"
    assert issued.status_code == 201, issued.text
    assert replay.status_code == 401
    events = cli_token_client.portal.call(
        cli_token_client.app.state.repositories.auth_audit.list_all
    )
    assert [event.operation for event in events[-4:]] == ["cli.login"] * 4
    assert [event.result for event in events[-4:]] == [
        "rejected",
        "rejected",
        "ok",
        "rejected",
    ]


def test_http_surface_has_cli_exchange_but_no_reset_or_recovery_codes(
    cli_token_client: TestClient,
) -> None:
    schema = cli_token_client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/admin/cli-tokens" in paths
    assert all("reset" not in path.lower() for path in paths)
    assert all("recovery" not in path.lower() for path in paths)
    assert all("recovery" not in name.lower() for name in schema["components"]["schemas"])


def test_external_cli_reset_is_observed_and_closes_all_authenticated_websockets(
    cli_token_client: TestClient,
) -> None:
    login = cli_token_client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": ADMIN_TOKEN},
    )
    assert login.status_code == 201
    cli_credential = cli_token_client.post(
        "/api/v1/admin/cli-tokens",
        json={"admin_token": ADMIN_TOKEN},
    ).json()["access_token"]
    native_key, native_jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(cli_token_client, native_jwk)
    approve_authorization(cli_token_client, transaction_id)
    native_tokens, native_nonce = exchange_authorization(
        cli_token_client,
        native_key,
        native_jwk,
        transaction_id,
        verifier,
    )
    native_access = str(native_tokens["access_token"])

    credentials: list[tuple[str, dict[str, str]]] = [
        ("web", {"Origin": ORIGIN}),
        ("cli", {"Authorization": f"Bearer {cli_credential}"}),
    ]
    provisioned = [_provision_instance(cli_token_client) for _ in range(3)]
    native_terminal_path = f"/api/v1/terms/{provisioned[2][0]}/terminal"
    credentials.append(
        (
            "native",
            {
                "Authorization": f"Bearer {native_access}",
                "DPoP": proof(
                    native_key,
                    native_jwk,
                    method="GET",
                    htu=f"{ORIGIN}{native_terminal_path}",
                    nonce=native_nonce,
                    access_token=native_access,
                ),
            },
        )
    )

    with ExitStack() as stack:
        terminals = [
            _open_terminal(
                stack,
                cli_token_client,
                instance_id,
                instance_token,
                headers,
            )
            for (_name, headers), (instance_id, instance_token) in zip(
                credentials,
                provisioned,
                strict=True,
            )
        ]
        event_path = f"/api/v1/events?instance_id={provisioned[0][0]}"
        event_credentials = [credentials[0][1], credentials[1][1]]
        event_credentials.append(
            {
                "Authorization": f"Bearer {native_access}",
                "DPoP": proof(
                    native_key,
                    native_jwk,
                    method="GET",
                    htu=f"{ORIGIN}/api/v1/events",
                    nonce=native_nonce,
                    access_token=native_access,
                ),
            }
        )
        events = [
            stack.enter_context(
                cli_token_client.websocket_connect(event_path, headers=headers)
            )
            for headers in event_credentials
        ]

        result = CliRunner().invoke(
            cli_app,
            ["auth", "totp", "reset"],
            input="y\n",
            env={
                "TERMFLOW_ADMIN_TOKEN": ADMIN_TOKEN,
                "TERMFLOW_DATABASE_URL": cli_token_client.app.state.settings.database_url,
            },
        )
        assert result.exit_code == 0, result.output
        deadline = time.monotonic() + 3
        while (
            cli_token_client.app.state.browser_sessions.epoch == 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert cli_token_client.app.state.browser_sessions.epoch == 2
        assert cli_token_client.app.state.browser_sessions.live_count == 0

        for websocket in [*terminals, *events]:
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4401

    assert cli_token_client.get("/api/v1/admin/session").status_code == 401
    assert cli_token_client.get(
        "/api/v1/dashboard",
        headers={"Authorization": f"Bearer {cli_credential}"},
    ).status_code == 401
    assert cli_token_client.get(
        "/api/v1/dashboard",
        headers={"Authorization": f"Bearer {native_access}"},
    ).status_code == 401
