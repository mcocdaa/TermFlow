import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from websockets.sync.client import connect


def _browser_cookie(system) -> str:
    with httpx.Client(base_url=system.base_url) as browser:
        response = browser.post(
            "/api/v1/admin/sessions",
            headers={"Origin": system.base_url},
            json={"admin_token": system.admin_token},
            timeout=2,
        )
        response.raise_for_status()
        assert response.json()["authenticated"] is True
        return "; ".join(f"{name}={value}" for name, value in browser.cookies.items())


def _terminal(system, instance_id: UUID, cookie: str):
    return connect(
        f"ws://127.0.0.1:{system.port}/api/v1/terms/{instance_id}/terminal",
        origin=system.base_url,
        additional_headers={"Cookie": cookie},
        ping_interval=None,
        open_timeout=3,
        close_timeout=2,
        max_size=2 * 1024 * 1024,
    )


def _wait_ready_and_redraw(connection, timeout: float = 8) -> tuple[dict, bytes]:
    deadline = time.monotonic() + timeout
    ready: dict | None = None
    redraw = bytearray()
    while time.monotonic() < deadline and (ready is None or not redraw):
        raw = connection.recv(timeout=max(0.05, deadline - time.monotonic()))
        if isinstance(raw, bytes):
            redraw.extend(raw)
        else:
            control = json.loads(raw)
            if control["type"] == "terminal.ready":
                ready = control
    assert ready is not None
    assert ready["rows"] >= 1 and ready["cols"] >= 1
    assert redraw
    return ready, bytes(redraw)


def _wait_for_output(connection, marker: bytes, timeout: float = 8) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        raw = connection.recv(timeout=max(0.05, deadline - time.monotonic()))
        if isinstance(raw, bytes):
            output.extend(raw)
            if marker in output:
                return bytes(output)
    raise AssertionError(f"terminal output did not contain marker of {len(marker)} bytes")


def _wait_for_control(connection, message_type: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = connection.recv(timeout=max(0.05, deadline - time.monotonic()))
        if isinstance(raw, str):
            control = json.loads(raw)
            if control.get("type") == message_type:
                return control
    raise AssertionError(f"terminal control frame {message_type!r} was not received")


def _wait_for_pane_count(system, instance_id: UUID, count: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        topology = system.topology(instance_id)
        panes = sum(len(window["panes"]) for window in topology["windows"])
        if panes == count:
            return
        time.sleep(0.05)
    raise AssertionError(f"topology did not reach {count} Panes")


@pytest.mark.e2e
@pytest.mark.tmux
def test_browser_cookie_controls_a_real_tmux_client_without_resizing_a(
    termflow_system,
) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("full-terminal")
    assert termflow_system.wait_until_online(instance.instance_id)
    cookie = _browser_cookie(termflow_system)
    marker = f"FULL_TMUX_{uuid4().hex}".encode()

    with _terminal(termflow_system, instance.instance_id, cookie) as terminal:
        ready, redraw = _wait_ready_and_redraw(terminal)
        assert b"\x1b[" in redraw
        original_size = (ready["rows"], ready["cols"])

        terminal.send(b"printf '" + marker + b"\\n'\r")
        assert marker in _wait_for_output(terminal, marker)

        active_pane = termflow_system.first_pane_id(instance.instance_id)
        terminal.send(
            json.dumps(
                {
                    "type": "terminal.action",
                    "action_id": str(uuid4()),
                    "action": "split_left_right",
                    "target_pane_id": active_pane,
                    "confirmed": False,
                }
            )
        )
        _wait_for_pane_count(termflow_system, instance.instance_id, 2)

        # C has no resize control message. The server-reported A grid remains authoritative.
        with _terminal(termflow_system, instance.instance_id, cookie) as replacement:
            replaced = _wait_for_control(terminal, "terminal.closed")
            assert replaced["reason"] == "replaced"
            replacement_ready, _ = _wait_ready_and_redraw(replacement)
            assert (replacement_ready["rows"], replacement_ready["cols"]) == original_size

    assert termflow_system.local_tmux_is_alive(instance)
    assert sum(
        len(window["panes"])
        for window in termflow_system.topology(instance.instance_id)["windows"]
    ) == 2

    inspected = [
        path
        for path in Path(termflow_system.root).rglob("*")
        if path.is_file() and not path.name.endswith(".sock")
    ]
    assert all(marker not in path.read_bytes() for path in inspected)
