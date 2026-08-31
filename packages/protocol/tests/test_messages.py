from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    BridgeHelloPayload,
    PaneOutputPayload,
    PaneSnapshot,
    TerminalActionPayload,
    TerminalInputPayload,
    TerminalOutputPayload,
    TermRenamePayload,
    TermRenameResultPayload,
    TopologySnapshot,
    WindowSnapshot,
    WireMessage,
    parse_payload,
)


def test_bridge_advertises_full_terminal_capability_by_default() -> None:
    assert "full_terminal" in BridgeHelloPayload(name="term").capabilities


def test_output_bytes_round_trip_as_base64() -> None:
    raw = b"\xff\x1b[31mred"
    payload = PaneOutputPayload.from_bytes("%1", uuid4(), 7, raw)
    assert payload.to_bytes() == raw
    assert payload.seq == 7


def test_output_rejects_malformed_base64() -> None:
    with pytest.raises(ValidationError):
        PaneOutputPayload(
            pane_id="%1",
            stream_id=uuid4(),
            seq=1,
            data_base64="not base64!",
            captured_at=datetime.now(UTC),
        )


def test_unknown_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WireMessage(
            protocol_version=2,
            message_id=uuid4(),
            type="bridge.heartbeat",
            instance_id=uuid4(),
            payload={},
        )


def test_tmux_ids_are_validated() -> None:
    with pytest.raises(ValidationError):
        PaneSnapshot(
            pane_id="1",
            window_id="@0",
            index=0,
            title="bad",
            width=80,
            height=24,
            active=True,
            dead=False,
        )


def test_topology_round_trip_preserves_hierarchy() -> None:
    pane = PaneSnapshot(
        pane_id="%1",
        window_id="@2",
        index=0,
        title="shell",
        width=80,
        height=24,
        active=True,
        dead=False,
    )
    topology = TopologySnapshot(
        session_id="$3",
        session_name="main",
        revision=4,
        windows=[WindowSnapshot(window_id="@2", index=0, name="main", active=True, panes=[pane])],
    )
    assert topology.windows[0].panes[0].pane_id == "%1"


def test_old_pane_snapshot_payload_gets_compatible_geometry_defaults() -> None:
    pane = PaneSnapshot.model_validate(
        {
            "pane_id": "%1",
            "window_id": "@2",
            "index": 0,
            "title": "shell",
            "width": 80,
            "height": 24,
            "active": True,
            "dead": False,
        }
    )

    assert pane.left == 0
    assert pane.top == 0
    assert pane.current_command is None


def test_pane_snapshot_serializes_raw_tmux_geometry_and_command() -> None:
    pane = PaneSnapshot(
        pane_id="%1",
        window_id="@2",
        index=0,
        title="shell",
        width=80,
        height=24,
        left=7,
        top=11,
        current_command="python -m worker",
        active=True,
        dead=False,
    )

    dumped = pane.model_dump()
    assert dumped["left"] == 7
    assert dumped["top"] == 11
    assert dumped["current_command"] == "python -m worker"


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        (
            "terminal.open",
            {
                "terminal_id": uuid4(),
                "resume_stream_id": uuid4(),
                "after_seq": 3,
            },
        ),
        (
            "terminal.opened",
            {
                "terminal_id": uuid4(),
                "stream_id": uuid4(),
                "rows": 24,
                "cols": 80,
            },
        ),
        (
            "terminal.input",
            {"terminal_id": uuid4(), "data_base64": "AAE="},
        ),
        (
            "terminal.output",
            {
                "terminal_id": uuid4(),
                "stream_id": uuid4(),
                "seq": 1,
                "data_base64": "AP8=",
            },
        ),
        (
            "terminal.size",
            {"terminal_id": uuid4(), "rows": 30, "cols": 120},
        ),
        (
            "terminal.bindings",
            {
                "terminal_id": uuid4(),
                "prefix": "C-b",
                "prefix2": None,
                "bindings": [
                    {
                        "action": "split_left_right",
                        "key": "C-b %",
                        "tooltip": "Split left/right",
                    }
                ],
            },
        ),
        (
            "terminal.action",
            {
                "terminal_id": uuid4(),
                "action_id": uuid4(),
                "action": "toggle_zoom",
                "target_pane_id": "%4",
                "confirmed": False,
            },
        ),
        (
            "terminal.action_result",
            {
                "terminal_id": uuid4(),
                "action_id": uuid4(),
                "ok": False,
                "error_code": "pane_not_found",
            },
        ),
        (
            "terminal.close",
            {"terminal_id": uuid4(), "reason": "client_closed"},
        ),
        (
            "terminal.closed",
            {"terminal_id": uuid4(), "reason": "grace_expired"},
        ),
    ],
)
def test_terminal_wire_payloads_are_strongly_typed(
    message_type: str,
    payload: dict[str, object],
) -> None:
    parsed = parse_payload(message_type, payload)
    assert parsed.terminal_id == payload["terminal_id"]


@pytest.mark.parametrize("payload_type", [TerminalInputPayload, TerminalOutputPayload])
def test_terminal_byte_payloads_reject_malformed_base64(payload_type: type[object]) -> None:
    common: dict[str, object] = {"terminal_id": uuid4(), "data_base64": "not base64!"}
    if payload_type is TerminalOutputPayload:
        common.update(stream_id=uuid4(), seq=1)
    with pytest.raises(ValidationError, match="strict Base64"):
        payload_type(**common)  # type: ignore[operator]


@pytest.mark.parametrize("payload_type", [TerminalInputPayload, TerminalOutputPayload])
def test_terminal_byte_payloads_reject_decoded_chunks_over_64_kib(
    payload_type: type[object],
) -> None:
    common: dict[str, object] = {
        "terminal_id": uuid4(),
        "data_base64": __import__("base64").b64encode(b"x" * 65_537).decode("ascii"),
    }
    if payload_type is TerminalOutputPayload:
        common.update(stream_id=uuid4(), seq=1)
    with pytest.raises(ValidationError, match="65536"):
        payload_type(**common)  # type: ignore[operator]


def test_terminal_output_round_trips_arbitrary_bytes() -> None:
    raw = bytes(range(256))
    payload = TerminalOutputPayload.from_bytes(uuid4(), uuid4(), 1, raw)
    assert payload.to_bytes() == raw


@pytest.mark.parametrize("action", ["open_shell", "ask_agent", "resize"])
def test_terminal_action_is_a_closed_set(action: str) -> None:
    with pytest.raises(ValidationError):
        TerminalActionPayload(
            terminal_id=uuid4(),
            action_id=uuid4(),
            action=action,
            target_pane_id="%1",
        )


def test_terminal_action_requires_target_for_pane_scoped_action() -> None:
    with pytest.raises(ValidationError, match="target_pane_id"):
        TerminalActionPayload(
            terminal_id=uuid4(),
            action_id=uuid4(),
            action="select_left",
        )


def test_close_pane_requires_explicit_confirmation() -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        TerminalActionPayload(
            terminal_id=uuid4(),
            action_id=uuid4(),
            action="close_pane",
            target_pane_id="%1",
            confirmed=False,
        )


def test_term_rename_command_and_result_are_strongly_typed() -> None:
    command_id = uuid4()
    command = parse_payload(
        "term.rename",
        {"command_id": command_id, "name": "工作区"},
    )
    result = parse_payload(
        "term.rename_result",
        {"command_id": command_id, "ok": True, "error_code": None},
    )
    assert isinstance(command, TermRenamePayload)
    assert command.name == "工作区"
    assert isinstance(result, TermRenameResultPayload)
    assert result.ok is True


@pytest.mark.parametrize("name", ["", "x" * 129, "bad\x00name", "bad\x85name"])
def test_term_rename_command_validates_display_name(name: str) -> None:
    with pytest.raises(ValidationError):
        TermRenamePayload(command_id=uuid4(), name=name)
