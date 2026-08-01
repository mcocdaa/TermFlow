import os

import pytest
from termflow_node.tmux.runner import TmuxRunner

pytestmark = pytest.mark.tmux


def test_private_tmux_server_survives_without_attached_client(tmp_path) -> None:
    socket_path = (tmp_path / "private" / "instance.sock").absolute()
    socket_path.parent.mkdir(mode=0o700)
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "termflow-test")
    try:
        assert runner.is_alive()
        assert runner.list_pane_ids() == ["%0"]
        assert socket_path.exists()
        assert os.stat(socket_path).st_uid == os.getuid()
    finally:
        runner.kill_server()
    assert not runner.is_alive()
