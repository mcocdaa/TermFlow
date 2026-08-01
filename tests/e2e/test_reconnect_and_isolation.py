import time

import pytest


@pytest.mark.e2e
@pytest.mark.tmux
def test_b_restart_does_not_stop_instance(termflow_system) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("restart-case")
    assert termflow_system.wait_until_online(instance.instance_id)

    termflow_system.stop_control_plane()
    assert termflow_system.local_tmux_is_alive(instance)
    termflow_system.start_control_plane()
    assert termflow_system.wait_until_online(instance.instance_id, timeout=10)
    assert termflow_system.topology(instance.instance_id)["windows"]


@pytest.mark.e2e
@pytest.mark.tmux
def test_two_instances_have_independent_processes_sockets_and_events(termflow_system) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    first = termflow_system.new_and_detach("first")
    second = termflow_system.new_and_detach("second")
    assert first.instance_id != second.instance_id
    assert first.socket_path != second.socket_path
    assert first.bridge_pid != second.bridge_pid
    assert termflow_system.wait_until_online(first.instance_id)
    assert termflow_system.wait_until_online(second.instance_id)

    first_pane = termflow_system.first_pane_id(first.instance_id)
    second_pane = termflow_system.first_pane_id(second.instance_id)
    first_events = termflow_system.subscribe(first.instance_id)
    second_events = termflow_system.subscribe(second.instance_id)
    try:
        termflow_system.send_text(first.instance_id, first_pane, "printf ONLY_A", submit=True)
        termflow_system.send_text(second.instance_id, second_pane, "printf ONLY_B", submit=True)
        first_output = first_events.wait_for_output(b"ONLY_A")
        second_output = second_events.wait_for_output(b"ONLY_B")
        assert first_output is not None and b"ONLY_B" not in first_output[1]
        assert second_output is not None and b"ONLY_A" not in second_output[1]
    finally:
        first_events.connection.close()
        second_events.connection.close()

    killed = termflow_system.run_node("kill", str(first.instance_id))
    assert killed.returncode == 0, killed.stdout + killed.stderr
    time.sleep(0.1)
    assert not termflow_system.local_tmux_is_alive(first)
    assert termflow_system.local_tmux_is_alive(second)
