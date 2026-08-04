from pathlib import Path
from subprocess import CompletedProcess

import pytest
from termflow_node.tmux import runner as runner_module
from termflow_node.tmux.runner import (
    SocketPathTooLong,
    TmuxCommandError,
    TmuxRunner,
    TmuxUnavailable,
    UnsupportedTmuxVersion,
)


def test_frozen_tmux_process_restores_original_library_path(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")

    monkeypatch.setattr(runner_module.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pyinstaller-private")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    runner_module._subprocess_run(
        ["tmux", "-V"], capture_output=True, text=True, check=False
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert "LD_LIBRARY_PATH_ORIG" not in environment


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


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("version check: tmux 3.2a (Linux)\n", ""),
        ("", "tmux 3.4\n"),
    ],
)
def test_tmux_version_accepts_wrapped_stdout_or_stderr(tmp_path, stdout, stderr) -> None:
    def version_run(argv, **kwargs):
        return CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    TmuxRunner((tmp_path / "tmux.sock").absolute(), run=version_run)


def test_tmux_version_failure_reports_bounded_process_diagnostics(tmp_path) -> None:
    def failed_version(argv, **kwargs):
        return CompletedProcess(
            argv,
            9,
            stdout="wrapper output\n",
            stderr="tmux launcher failed " + "x" * 300,
        )

    with pytest.raises(TmuxUnavailable) as caught:
        TmuxRunner((tmp_path / "tmux.sock").absolute(), run=failed_version)

    detail = str(caught.value)
    assert "exit=9" in detail
    assert "wrapper output" in detail
    assert "tmux launcher failed" in detail
    assert len(detail) < 450


def test_tmux_version_rejects_unparseable_and_old_versions(tmp_path) -> None:
    def unparseable(argv, **kwargs):
        return CompletedProcess(argv, 0, stdout="tmux next-3.5\n", stderr="")

    with pytest.raises(TmuxUnavailable, match="Unable to determine tmux version"):
        TmuxRunner((tmp_path / "unparseable.sock").absolute(), run=unparseable)

    def old_version(argv, **kwargs):
        return CompletedProcess(argv, 0, stdout="tmux 3.1\n", stderr="")

    with pytest.raises(UnsupportedTmuxVersion, match=r"tmux 3\.2\+ is required; found 3\.1"):
        TmuxRunner((tmp_path / "old.sock").absolute(), run=old_version)


def test_session_identity_rename_kill_and_attach_use_argv_targets(tmp_path) -> None:
    class IdentityRun(FakeRun):
        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            if argv == ["tmux", "-V"]:
                return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")
            if "display-message" in argv:
                return CompletedProcess(argv, 0, stdout="$7\t真实名称\n", stderr="")
            return CompletedProcess(argv, 0, stdout="", stderr="")

    fake_run = IdentityRun()
    socket_path = (tmp_path / "tmux.sock").absolute()
    runner = TmuxRunner(socket_path, run=fake_run)
    identity = runner.session_identity("$7")
    runner.rename_session("$7", "name with spaces;$(safe)")
    runner.kill_session("$7")

    assert identity.session_id == "$7"
    assert identity.session_name == "真实名称"
    assert runner.attach_argv("$7")[-2:] == ["-t", "$7"]
    assert fake_run.calls[-2] == [
        "tmux",
        "-S",
        str(socket_path),
        "rename-session",
        "-t",
        "$7",
        "name with spaces;$(safe)",
    ]
    assert fake_run.calls[-1][-3:] == ["kill-session", "-t", "$7"]


def test_list_clients_parses_raw_tmux_client_fields(tmp_path) -> None:
    class ClientRun(FakeRun):
        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            if argv == ["tmux", "-V"]:
                return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")
            return CompletedProcess(
                argv,
                0,
                stdout="/dev/pts/1\t123\t120\t40\t0\txterm-256color\n",
                stderr="",
            )

    runner = TmuxRunner((tmp_path / "tmux.sock").absolute(), run=ClientRun())
    clients = runner.list_clients("$0")
    assert clients[0].tty == "/dev/pts/1"
    assert clients[0].activity == 123
    assert clients[0].cols == 120
    assert clients[0].rows == 40
    assert clients[0].control_mode is False
    assert clients[0].termname == "xterm-256color"


def test_list_clients_tolerates_control_mode_client_without_a_grid(tmp_path) -> None:
    class ClientRun(FakeRun):
        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            if argv == ["tmux", "-V"]:
                return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")
            return CompletedProcess(
                argv,
                0,
                stdout=(
                    "/dev/pts/control\t123\t\t\t1\t\n"
                    "/dev/pts/local\t124\t100\t30\t0\txterm-256color\n"
                ),
                stderr="",
            )

    runner = TmuxRunner((tmp_path / "tmux.sock").absolute(), run=ClientRun())

    clients = runner.list_clients("$0")

    assert clients[0].control_mode is True
    assert (clients[0].rows, clients[0].cols) == (0, 0)
    assert (clients[1].rows, clients[1].cols) == (30, 100)
