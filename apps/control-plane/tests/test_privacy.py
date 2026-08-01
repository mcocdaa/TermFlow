import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

from termflow_protocol import (
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
