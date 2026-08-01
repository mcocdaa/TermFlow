import pytest
from termflow_node.tmux.runner import TmuxRunner

pytestmark = pytest.mark.tmux


def test_real_tmux_session_remains_attachable_by_id_after_rename(tmp_path) -> None:
    socket_path = (tmp_path / "identity.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("initial name", "window")
    try:
        identity = runner.session_identity()
        assert identity.session_name == "initial name"
        runner.rename_session(identity.session_id, "renamed 本地")
        renamed = runner.session_identity(identity.session_id)
        assert renamed.session_id == identity.session_id
        assert renamed.session_name == "renamed 本地"
        assert runner.is_alive(identity.session_id)
        assert runner.attach_argv(identity.session_id)[-1] == identity.session_id
    finally:
        runner.kill_server()
