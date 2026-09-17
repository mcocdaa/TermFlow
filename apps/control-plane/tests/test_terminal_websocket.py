from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    TerminalActionResultPayload,
    TerminalBindingsPayload,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    TerminalSizePayload,
    WireMessage,
)

ORIGIN = "http://127.0.0.1:8000"


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
    provision_term,
) -> None:
    term = provision_term(name="terminal")
    instance_id, instance_token = term.instance_id, term.instance_token
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
            action_result = terminal.receive_json()
            assert action_result == {
                "type": "terminal.action_result",
                "terminal_id": str(terminal_id),
                "action_id": str(action_id),
                "ok": False,
                "error_code": "target_not_found",
            }
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


def test_terminal_auth_origin_offline_and_native_bearer(
    client,
    admin_headers,
    provision_term,
) -> None:
    term = provision_term(name="terminal")
    instance_id, instance_token = term.instance_id, term.instance_token
    _login(client)
    url = f"/api/v1/terms/{instance_id}/terminal"

    with pytest.raises(WebSocketDisconnect) as invalid_origin:
        with client.websocket_connect(url, headers={"Origin": "https://evil.example"}) as rejected:
            rejected.receive_json()
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
