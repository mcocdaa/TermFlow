from pathlib import Path

import pytest

SENTINEL = b"SECRET_E2E_TERMINAL_38e6"


@pytest.mark.e2e
@pytest.mark.tmux
def test_live_terminal_body_is_absent_from_b_and_node_files(termflow_system) -> None:
    enrollment = termflow_system.create_enrollment()
    termflow_system.login(enrollment)
    instance = termflow_system.new_and_detach("privacy")
    assert termflow_system.wait_until_online(instance.instance_id)
    pane_id = termflow_system.first_pane_id(instance.instance_id)
    events = termflow_system.subscribe(instance.instance_id)
    try:
        termflow_system.send_text(
            instance.instance_id,
            pane_id,
            f"printf {SENTINEL.decode()}",
            submit=True,
        )
        assert events.wait_for_bytes(SENTINEL)
    finally:
        events.connection.close()

    inspected = [
        path
        for path in Path(termflow_system.root).rglob("*")
        if path.is_file() and not path.name.endswith(".sock")
    ]
    assert all(SENTINEL not in path.read_bytes() for path in inspected)
    b_files = [
        path
        for path in inspected
        if path.name.startswith("control-plane")
    ]
    registered = termflow_system.instance_store.load(instance.instance_id)
    assert registered.instance_token is not None
    instance_token = registered.instance_token.get_secret_value().encode()
    assert all(enrollment.encode() not in path.read_bytes() for path in b_files)
    assert all(instance_token not in path.read_bytes() for path in b_files)
