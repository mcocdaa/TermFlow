import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.audit import (
    AuthAuditOperation,
    AuthAuditResult,
    AuthenticationAudit,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_protocol import (
    BridgeHelloPayload,
    CommandResultPayload,
    MessageType,
    PaneSnapshot,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)

SENTINEL = "SECRET_TERMINAL_BODY_9f0d"
FULL_TERMINAL_SENTINEL = b"FULL_PTY_SECRET_BODY_71ac"
AUTH_SOURCE_SENTINEL = "203.0.113.42"
AUTH_CREDENTIAL_SENTINELS = (
    "ADMIN_TOKEN_SECRET_5ad0",
    "TOTP_SECRET_194203",
    "DPoP_PROOF_SECRET_218a",
    "REFRESH_TOKEN_SECRET_b093",
)


class _AuthAuditRepository:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    async def record(
        self,
        operation: str,
        result: str,
        source_digest: str,
        *,
        client_id: UUID | None = None,
        error_code: str | None = None,
    ) -> object:
        row = {
            "operation": operation,
            "result": result,
            "source_digest": source_digest,
            "client_id": client_id,
            "error_code": error_code,
        }
        self.rows.append(row)
        return row


@pytest.mark.asyncio
async def test_auth_audit_contains_only_allowlisted_secret_free_metadata() -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    repository = _AuthAuditRepository()
    audit = AuthenticationAudit(
        repository,
        digest_key=b"audit-key-that-never-appears-in-repr",
        clock=lambda: now,
    )

    event = await audit.record(
        AuthAuditOperation.WEB_SESSION_LOGIN,
        AuthAuditResult.REJECTED,
        AUTH_SOURCE_SENTINEL,
    )

    assert event.operation is AuthAuditOperation.WEB_SESSION_LOGIN
    assert event.result is AuthAuditResult.REJECTED
    assert event.occurred_at == now
    assert event.source_digest == repository.rows[0]["source_digest"]
    assert len(event.source_digest) == 32
    rendered = repr(event) + repr(repository.rows) + repr(audit)
    assert AUTH_SOURCE_SENTINEL not in rendered
    assert "audit-key-that-never-appears-in-repr" not in rendered
    assert all(secret not in rendered for secret in AUTH_CREDENTIAL_SENTINELS)


def test_request_validation_error_never_echoes_submitted_credentials(client, caplog) -> None:
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": "http://127.0.0.1:8000"},
        json={"admin_token": {"submitted": AUTH_CREDENTIAL_SENTINELS[0]}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["message"] == "The request is invalid."
    rendered = response.text + caplog.text
    assert all(secret not in rendered for secret in AUTH_CREDENTIAL_SENTINELS)


def test_auth_audit_does_not_pollute_terminal_interaction_statistics(client) -> None:
    before = client.portal.call(client.app.state.repositories.audit.list_all)
    client.portal.call(
        client.app.state.auth_audit.record,
        AuthAuditOperation.WEB_SESSION_LOGIN,
        AuthAuditResult.REJECTED,
        AUTH_SOURCE_SENTINEL,
    )
    after = client.portal.call(client.app.state.repositories.audit.list_all)

    assert before == after


def test_terminal_body_and_raw_credentials_never_reach_sqlite_or_logs(
    client,
    admin_headers,
    settings,
    caplog,
) -> None:
    enrollment_token = client.post(
        "/api/v1/enrollment-tokens",
        headers=admin_headers,
    ).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment_token},
    ).json()
    instance_id = uuid4()
    registration = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "private"},
    ).json()
    raw_tokens = {
        enrollment_token,
        installation["installation_token"],
        registration["instance_token"],
    }
    topology = TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    )
                ],
            )
        ],
    )
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {registration['instance_token']}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.TOPOLOGY_SNAPSHOT,
                instance_id=instance_id,
                payload=TopologySnapshotPayload(topology=topology).model_dump(mode="json"),
            ).model_dump_json()
        )
        idempotency_key = uuid4()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                client.post,
                f"/api/v1/instances/{instance_id}/panes/%251/input",
                headers={**admin_headers, "Idempotency-Key": str(idempotency_key)},
                json={"text": SENTINEL, "submit": True},
            )
            incoming = WireMessage.model_validate(bridge.receive_json())
            assert incoming.payload["text"] == SENTINEL
            command_id = UUID(str(incoming.payload["command_id"]))
            bridge.send_text(
                WireMessage(
                    type=MessageType.COMMAND_RESULT,
                    instance_id=instance_id,
                    payload=CommandResultPayload(
                        command_id=command_id,
                        idempotency_key=idempotency_key,
                        ok=True,
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            assert pending.result(timeout=2).status_code == 200

    database_path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        audit_rows = database.execute(
            "SELECT operation, pane_id, input_bytes, result, error_code FROM audit_events"
        ).fetchall()
    assert audit_rows[-1] == ("pane.input", "%1", len(SENTINEL.encode()), "ok", None)

    logs = caplog.text
    assert SENTINEL not in logs
    assert all(token not in logs for token in raw_tokens)
    database_bytes = database_path.read_bytes()
    assert SENTINEL.encode() not in database_bytes
    assert all(token.encode() not in database_bytes for token in raw_tokens)


def test_browser_session_secret_is_absent_from_logs_responses_and_store_repr(
    client,
    caplog,
) -> None:
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": "http://127.0.0.1:8000"},
        json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
    )
    raw_cookie = response.cookies.get("termflow_session")
    assert raw_cookie
    status = client.get("/api/v1/admin/session")
    assert raw_cookie not in status.text
    assert raw_cookie not in caplog.text
    assert raw_cookie not in repr(client.app.state.browser_sessions)


def test_pending_totp_secret_is_encrypted_and_absent_from_logs_and_repr(
    tmp_path,
    caplog,
) -> None:
    database_path = tmp_path / "totp-privacy.db"
    encoded_key = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as test_client:
        assert test_client.post(
            "/api/v1/admin/sessions",
            headers={"Origin": "http://127.0.0.1:8000"},
            json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
        ).status_code == 201
        setup = test_client.post(
            "/api/v1/admin/totp/setups",
            headers={"Origin": "http://127.0.0.1:8000"},
            json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
        )
        setup_key = setup.json()["setup_key"]
        raw_secret = base64.b32decode(setup_key)
        rendered = repr(test_client.app.state.authentication_service) + caplog.text

    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = database_path.read_bytes()
    assert setup.status_code == 201
    assert setup.headers["cache-control"] == "no-store"
    assert setup_key not in rendered
    assert raw_secret not in database_bytes
    assert setup_key.encode() not in database_bytes


def test_full_terminal_bytes_are_ephemeral_but_byte_count_is_audited(
    client,
    admin_headers,
    settings,
    caplog,
) -> None:
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
        json={"instance_id": str(instance_id), "name": "full-private"},
    ).json()

    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {registration['instance_token']}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="full-private").model_dump(mode="json"),
            ).model_dump_json()
        )
        with client.websocket_connect(
            f"/api/v1/terms/{instance_id}/terminal",
            headers=admin_headers,
        ) as terminal:
            opened_request = WireMessage.model_validate(bridge.receive_json())
            terminal_id = UUID(str(opened_request.payload["terminal_id"]))
            stream_id = uuid4()
            bridge.send_text(
                WireMessage(
                    type=MessageType.TERMINAL_OPENED,
                    instance_id=instance_id,
                    payload=TerminalOpenedPayload(
                        terminal_id=terminal_id,
                        stream_id=stream_id,
                        rows=24,
                        cols=80,
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            terminal.receive_json()
            split_at = len(FULL_TERMINAL_SENTINEL) // 2
            for chunk in (
                FULL_TERMINAL_SENTINEL[:split_at],
                FULL_TERMINAL_SENTINEL[split_at:],
            ):
                terminal.send_bytes(chunk)
                incoming = WireMessage.model_validate(bridge.receive_json())
                assert incoming.type is MessageType.TERMINAL_INPUT

            bridge.send_text(
                WireMessage(
                    type=MessageType.TERMINAL_OUTPUT,
                    instance_id=instance_id,
                    payload=TerminalOutputPayload.from_bytes(
                        terminal_id,
                        stream_id,
                        1,
                        FULL_TERMINAL_SENTINEL,
                    ).model_dump(mode="json"),
                ).model_dump_json()
            )
            assert terminal.receive_bytes() == FULL_TERMINAL_SENTINEL
            terminal.send_json({"type": "terminal.close", "reason": "client_closed"})
            closing = WireMessage.model_validate(bridge.receive_json())
            assert closing.type is MessageType.TERMINAL_CLOSE

    audit_rows = client.portal.call(client.app.state.repositories.audit.list_all)
    input_rows = [row for row in audit_rows if row.operation == "terminal.input"]
    assert len(input_rows) == 1
    assert input_rows[-1].input_bytes == len(FULL_TERMINAL_SENTINEL)
    assert not hasattr(input_rows[-1], "text")

    database_path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert FULL_TERMINAL_SENTINEL not in database_path.read_bytes()
    assert FULL_TERMINAL_SENTINEL.decode() not in caplog.text
