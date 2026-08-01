import pytest


@pytest.mark.e2e
@pytest.mark.tmux
def test_enroll_create_detach_and_remote_control(termflow_system) -> None:
    enrollment = termflow_system.create_enrollment()
    termflow_system.login(enrollment)
    instance = termflow_system.new_and_detach("e2e-main")

    assert termflow_system.wait_until_online(instance.instance_id)
    pane_id = termflow_system.first_pane_id(instance.instance_id)
    event_cursor = termflow_system.subscribe(instance.instance_id)
    try:
        result = termflow_system.send_text(
            instance.instance_id,
            pane_id,
            "printf E2E_REMOTE_OK",
            submit=True,
        )
        assert result["ok"] is True
        assert event_cursor.wait_for_bytes(b"E2E_REMOTE_OK", timeout=5)
    finally:
        event_cursor.connection.close()
    assert termflow_system.local_tmux_is_alive(instance)
