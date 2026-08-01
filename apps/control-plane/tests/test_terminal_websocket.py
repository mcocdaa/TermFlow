from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    TerminalActionResultPayload,
    TerminalBindingsPayload,
    TerminalClosedPayload,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    TerminalSizePayload,
    WireMessage,
)

ORIGIN = "http://127.0.0.1:8000"


def _provision(client, admin_headers):
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
        json={"instance_id": str(instance_id), "name": "terminal"},
    ).json()
    return instance_id, registration["instance_token"]


def _login(client) -> None:
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
    )
    assert response.status_code == 201


def _bridge_message(instance_id, message_type: MessageType, payload) -> str:
    return WireMessage(
        type=message_type,
        instance_id=instance_id,
        payload=payload.model_dump(mode="json"),
    ).model_dump_json()


def _announce_bridge(bridge, instance_id: UUID) -> None:
    bridge.send_text(
        _bridge_message(
            instance_id,
            MessageType.BRIDGE_HELLO,
            BridgeHelloPayload(name="terminal"),
        )
    )


def test_browser_terminal_routes_binary_and_semantic_control_frames(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    _login(client)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        _announce_bridge(bridge, instance_id)
        with client.websocket_connect(
            f"/api/v1/terms/{instance_id}/terminal",
            headers={"Origin": ORIGIN},
        ) as terminal:
            opened_request = WireMessage.model_validate(bridge.receive_json())
            assert opened_request.type is MessageType.TERMINAL_OPEN
            terminal_id = UUID(str(opened_request.payload["terminal_id"]))
            stream_id = uuid4()
            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_OPENED,
                    TerminalOpenedPayload(
                        terminal_id=terminal_id,
                        stream_id=stream_id,
                        rows=24,
                        cols=80,
                    ),
                )
            )
            assert terminal.receive_json() == {
                "type": "terminal.ready",
                "terminal_id": str(terminal_id),
                "stream_id": str(stream_id),
                "rows": 24,
                "cols": 80,
            }

            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_OUTPUT,
                    TerminalOutputPayload.from_bytes(
                        terminal_id,
                        stream_id,
                        1,
                        b"screen\x00\xff",
                    ),
                )
            )
            assert terminal.receive_bytes() == b"screen\x00\xff"

            terminal.send_bytes(b"input\x00\xff")
            incoming = WireMessage.model_validate(bridge.receive_json())
            assert incoming.type is MessageType.TERMINAL_INPUT
            assert incoming.payload["terminal_id"] == str(terminal_id)
            assert incoming.payload["data_base64"] == "aW5wdXQA/w=="

            action_id = uuid4()
            terminal.send_json(
                {
                    "type": "terminal.action",
                    "action_id": str(action_id),
                    "action": "toggle_zoom",
                    "target_pane_id": "%1",
                    "confirmed": False,
                }
            )
            action = WireMessage.model_validate(bridge.receive_json())
            assert action.type is MessageType.TERMINAL_ACTION
            assert action.payload["action_id"] == str(action_id)
            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_ACTION_RESULT,
                    TerminalActionResultPayload(
                        terminal_id=terminal_id,
                        action_id=action_id,
                        ok=False,
                        error_code="target_not_found",
                    ),
                )
            )
            assert terminal.receive_json()["code"] == "target_not_found"

            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_SIZE,
                    TerminalSizePayload(terminal_id=terminal_id, rows=30, cols=100),
                )
            )
            assert terminal.receive_json()["type"] == "terminal.size"
            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_BINDINGS,
                    TerminalBindingsPayload(
                        terminal_id=terminal_id,
                        prefix="C-b",
                        prefix2=None,
                        bindings=[],
                    ),
                )
            )
            assert terminal.receive_json()["type"] == "terminal.binding_snapshot"

            terminal.send_json({"type": "terminal.close", "reason": "client_closed"})
            closing = WireMessage.model_validate(bridge.receive_json())
            assert closing.type is MessageType.TERMINAL_CLOSE
            assert closing.payload["reason"] == "client_closed"


def test_terminal_auth_origin_offline_and_native_bearer(client, admin_headers) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    _login(client)
    url = f"/api/v1/terms/{instance_id}/terminal"

    with pytest.raises(WebSocketDisconnect) as invalid_origin:
        with client.websocket_connect(url, headers={"Origin": "https://evil.example"}):
            pass
    assert invalid_origin.value.code == 4403

    with client.websocket_connect(url, headers={"Origin": ORIGIN}) as offline:
        error = offline.receive_json()
        assert error["type"] == "terminal.error"
        assert error["code"] == "instance_offline"
        with pytest.raises(WebSocketDisconnect):
            offline.receive_json()

    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        _announce_bridge(bridge, instance_id)
        with client.websocket_connect(url, headers=admin_headers):
            opening = WireMessage.model_validate(bridge.receive_json())
            assert opening.type is MessageType.TERMINAL_OPEN


def test_terminal_rejects_bridge_without_full_terminal_capability(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            _bridge_message(
                instance_id,
                MessageType.BRIDGE_HELLO,
                BridgeHelloPayload(name="old-node", capabilities=("topology",)),
            )
        )
        with client.websocket_connect(
            f"/api/v1/terms/{instance_id}/terminal",
            headers=admin_headers,
        ) as terminal:
            assert terminal.receive_json()["code"] == "capability_unavailable"
            with pytest.raises(WebSocketDisconnect):
                terminal.receive_json()


def test_new_owner_replaces_old_and_logout_closes_cookie_terminal(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    _login(client)
    url = f"/api/v1/terms/{instance_id}/terminal"
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        _announce_bridge(bridge, instance_id)
        with client.websocket_connect(url, headers={"Origin": ORIGIN}) as first:
            first_open = WireMessage.model_validate(bridge.receive_json())
            with client.websocket_connect(url, headers={"Origin": ORIGIN}) as second:
                second_open = WireMessage.model_validate(bridge.receive_json())
                assert first_open.payload["terminal_id"] != second_open.payload["terminal_id"]
                assert first.receive_json()["reason"] == "replaced"

                with ThreadPoolExecutor(max_workers=1) as executor:
                    logout = executor.submit(
                        client.delete,
                        "/api/v1/admin/session",
                        headers={"Origin": ORIGIN},
                    )
                    assert second.receive_json()["reason"] == "client_closed"
                    assert logout.result(timeout=2).status_code == 200


def test_session_capacity_eviction_closes_established_cookie_terminal(tmp_path) -> None:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'eviction.db'}",
        allow_insecure_loopback=True,
        browser_session_capacity=1,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"}
        instance_id, instance_token = _provision(client, headers)
        _login(client)
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {instance_token}"},
        ) as bridge:
            _announce_bridge(bridge, instance_id)
            with client.websocket_connect(
                f"/api/v1/terms/{instance_id}/terminal",
                headers={"Origin": ORIGIN},
            ) as terminal:
                WireMessage.model_validate(bridge.receive_json())
                _login(client)
                assert terminal.receive_json()["reason"] == "client_closed"


def test_plain_websocket_disconnect_preserves_aggregate_input_audit(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        _announce_bridge(bridge, instance_id)
        with client.websocket_connect(
            f"/api/v1/terms/{instance_id}/terminal",
            headers=admin_headers,
        ) as terminal:
            WireMessage.model_validate(bridge.receive_json())
            terminal.send_bytes(b"disconnect audit")
            assert (
                WireMessage.model_validate(bridge.receive_json()).type
                is MessageType.TERMINAL_INPUT
            )

        assert (
            WireMessage.model_validate(bridge.receive_json()).type
            is MessageType.TERMINAL_CLOSE
        )
        client.portal.call(client.app.state.terminal_audit.flush)

    audit_rows = client.portal.call(client.app.state.repositories.audit.list_all)
    input_rows = [row for row in audit_rows if row.operation == "terminal.input"]
    assert len(input_rows) == 1
    assert input_rows[0].input_bytes == len(b"disconnect audit")


def test_oversized_binary_and_json_terminal_bytes_close_only_that_terminal(
    tmp_path,
) -> None:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'small.db'}",
        allow_insecure_loopback=True,
        terminal_max_frame_bytes=8,
        terminal_input_rate_bytes_per_second=4,
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"}
        instance_id, instance_token = _provision(client, headers)
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {instance_token}"},
        ) as bridge:
            _announce_bridge(bridge, instance_id)
            url = f"/api/v1/terms/{instance_id}/terminal"
            with client.websocket_connect(url, headers=headers) as terminal:
                WireMessage.model_validate(bridge.receive_json())
                terminal.send_bytes(b"123456789")
                assert terminal.receive_json()["code"] == "frame_too_large"
                assert terminal.receive_json()["reason"] == "internal_error"

            with client.websocket_connect(url, headers=headers) as terminal:
                WireMessage.model_validate(bridge.receive_json())
                terminal.send_json(
                    {"type": "terminal.input", "data_base64": "U0VDUkVU"}
                )
                assert terminal.receive_json()["code"] == "invalid_control_frame"
                assert terminal.receive_json()["reason"] == "internal_error"

            with client.websocket_connect(url, headers=headers) as terminal:
                WireMessage.model_validate(bridge.receive_json())
                terminal.send_bytes(b"12345")
                assert terminal.receive_json()["code"] == "input_rate_exceeded"
                assert terminal.receive_json()["reason"] == "internal_error"


def test_bridge_closed_event_is_forwarded_as_browser_control(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        _announce_bridge(bridge, instance_id)
        with client.websocket_connect(
            f"/api/v1/terms/{instance_id}/terminal",
            headers=admin_headers,
        ) as terminal:
            request = WireMessage.model_validate(bridge.receive_json())
            terminal_id = UUID(str(request.payload["terminal_id"]))
            bridge.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_CLOSED,
                    TerminalClosedPayload(
                        terminal_id=terminal_id,
                        reason="grace_expired",
                    ),
                )
            )
            assert terminal.receive_json()["reason"] == "grace_expired"


def test_bridge_reconnect_requests_only_proven_terminal_stream_cursor(
    client,
    admin_headers,
) -> None:
    instance_id, instance_token = _provision(client, admin_headers)
    bridge_headers = {"Authorization": f"Bearer {instance_token}"}
    url = f"/api/v1/terms/{instance_id}/terminal"
    first_context = client.websocket_connect(
        "/api/v1/bridge/connect",
        headers=bridge_headers,
    )
    first_bridge = first_context.__enter__()
    _announce_bridge(first_bridge, instance_id)
    first_connected = True
    terminal_context = client.websocket_connect(url, headers=admin_headers)
    terminal = terminal_context.__enter__()
    try:
        opened_request = WireMessage.model_validate(first_bridge.receive_json())
        terminal_id = UUID(str(opened_request.payload["terminal_id"]))
        stream_id = uuid4()
        first_bridge.send_text(
            _bridge_message(
                instance_id,
                MessageType.TERMINAL_OPENED,
                TerminalOpenedPayload(
                    terminal_id=terminal_id,
                    stream_id=stream_id,
                    rows=24,
                    cols=80,
                ),
            )
        )
        terminal.receive_json()
        first_bridge.send_text(
            _bridge_message(
                instance_id,
                MessageType.TERMINAL_OUTPUT,
                TerminalOutputPayload.from_bytes(
                    terminal_id,
                    stream_id,
                    1,
                    b"one",
                ),
            )
        )
        assert terminal.receive_bytes() == b"one"

        first_context.__exit__(None, None, None)
        first_connected = False
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers=bridge_headers,
        ) as reconnected:
            _announce_bridge(reconnected, instance_id)
            resume = WireMessage.model_validate(reconnected.receive_json())
            assert resume.type is MessageType.TERMINAL_OPEN
            assert resume.payload == {
                "terminal_id": str(terminal_id),
                "resume_stream_id": str(stream_id),
                "after_seq": 1,
            }
            # A can emit a live chunk before it processes B's resume request. The
            # replay of that same sequence must be deduplicated, not treated as a gap.
            reconnected.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_OUTPUT,
                    TerminalOutputPayload.from_bytes(
                        terminal_id,
                        stream_id,
                        2,
                        b"two",
                    ),
                )
            )
            assert terminal.receive_bytes() == b"two"
            reconnected.send_text(
                _bridge_message(
                    instance_id,
                    MessageType.TERMINAL_OPENED,
                    TerminalOpenedPayload(
                        terminal_id=terminal_id,
                        stream_id=stream_id,
                        rows=24,
                        cols=80,
                    ),
                )
            )
            assert terminal.receive_json()["type"] == "terminal.ready"
            for seq, data in ((2, b"two"), (3, b"three")):
                reconnected.send_text(
                    _bridge_message(
                        instance_id,
                        MessageType.TERMINAL_OUTPUT,
                        TerminalOutputPayload.from_bytes(
                            terminal_id,
                            stream_id,
                            seq,
                            data,
                        ),
                    )
                )
            assert terminal.receive_bytes() == b"three"
    finally:
        if first_connected:
            first_context.__exit__(None, None, None)
        terminal_context.__exit__(None, None, None)
