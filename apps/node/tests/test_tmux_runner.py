from pathlib import Path
from subprocess import CompletedProcess

import pytest
from termflow_node.tmux.runner import SocketPathTooLong, TmuxCommandError, TmuxRunner


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ["tmux", "-V"]:
            return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")
        return CompletedProcess(argv, 0, stdout="", stderr="")


def test_tmux_commands_use_explicit_socket_and_never_a_shell(tmp_path) -> None:
    fake_run = FakeRun()
    socket_path = (tmp_path / "tmux.sock").absolute()
    runner = TmuxRunner(socket_path, run=fake_run)
    runner.create_session("main", "project-a")
    assert fake_run.calls == [
        ["tmux", "-V"],
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "main",
            "-n",
            "project-a",
        ],
    ]


def test_command_error_exposes_only_argv_and_exit_code(tmp_path) -> None:
    def failing(argv, **kwargs):
        if argv == ["tmux", "-V"]:
            return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")
        return CompletedProcess(argv, 1, stdout="pane secret", stderr="pane secret")

    runner = TmuxRunner((tmp_path / "tmux.sock").absolute(), run=failing)
    with pytest.raises(TmuxCommandError) as caught:
        runner.create_session("main", "project-a")
    assert caught.value.exit_code == 1
    assert "pane secret" not in str(caught.value)


def test_overlong_socket_path_is_rejected_before_spawn(tmp_path) -> None:
    path = Path("/") / ("a" * 108)
    with pytest.raises(SocketPathTooLong):
        TmuxRunner(path)
