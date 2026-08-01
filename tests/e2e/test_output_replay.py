import pytest


@pytest.mark.e2e
@pytest.mark.tmux
def test_subscriber_reconnect_replays_buffered_output(termflow_system) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("replay")
    assert termflow_system.wait_until_online(instance.instance_id)
    pane_id = termflow_system.first_pane_id(instance.instance_id)

    first_cursor = termflow_system.subscribe(instance.instance_id)
    termflow_system.send_text(instance.instance_id, pane_id, "printf REPLAY_ONE", submit=True)
    first = first_cursor.wait_for_output(b"REPLAY_ONE")
    first_cursor.connection.close()
    assert first is not None
    first_payload = first[0]["payload"]

    termflow_system.send_text(instance.instance_id, pane_id, "printf REPLAY_TWO", submit=True)
    replay = termflow_system.subscribe(
        instance.instance_id,
        pane_id=pane_id,
        stream_id=first_payload["stream_id"],
        after_seq=first_payload["seq"],
    )
    try:
        assert replay.wait_for_bytes(b"REPLAY_TWO", timeout=5)
    finally:
        replay.connection.close()
