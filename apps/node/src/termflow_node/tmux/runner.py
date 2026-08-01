"""Subprocess-safe tmux lifecycle calls for one private server."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Protocol


class TmuxUnavailable(RuntimeError):
    pass


class UnsupportedTmuxVersion(RuntimeError):
    pass


class SocketPathTooLong(ValueError):
    pass


class TmuxCommandError(RuntimeError):
    def __init__(self, argv: list[str], exit_code: int) -> None:
        self.argv = tuple(argv)
        self.exit_code = exit_code
        super().__init__(f"tmux command failed with exit code {exit_code}: {self.argv!r}")


class RunCommand(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


_VERSION = re.compile(r"^tmux\s+(\d+)\.(\d+)")


def _subprocess_run(
    argv: list[str],
    *,
    capture_output: bool,
    text: bool,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=capture_output,
        text=text,
        check=check,
    )


class TmuxRunner:
    def __init__(
        self,
        socket_path: Path,
        *,
        run: RunCommand = _subprocess_run,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("tmux socket path must be absolute")
        limit = 103 if sys.platform == "darwin" else 107
        if len(os.fsencode(socket_path)) > limit:
            raise SocketPathTooLong(f"tmux socket path exceeds {limit} bytes")
        self.socket_path = socket_path
        self._run = run
        self._verify_version()

    def _verify_version(self) -> None:
        try:
            result = self._run(
                ["tmux", "-V"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TmuxUnavailable("tmux is not installed") from exc
        match = _VERSION.match(result.stdout.strip()) if result.returncode == 0 else None
        if match is None:
            raise TmuxUnavailable("Unable to determine tmux version")
        version = (int(match.group(1)), int(match.group(2)))
        if version < (3, 2):
            raise UnsupportedTmuxVersion(f"tmux 3.2+ is required; found {version[0]}.{version[1]}")

    def _argv(self, *arguments: str) -> list[str]:
        return ["tmux", "-S", str(self.socket_path), *arguments]

    def _execute(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = self._argv(*arguments)
        result = self._run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise TmuxCommandError(argv, result.returncode)
        return result

    def create_session(self, session_name: str, window_name: str) -> None:
        self._execute(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-n",
            window_name,
        )

    def is_alive(self, session_name: str = "main") -> bool:
        return self._execute("has-session", "-t", session_name, check=False).returncode == 0

    def list_pane_ids(self) -> list[str]:
        result = self._execute("list-panes", "-a", "-F", "#{pane_id}")
        return [line for line in result.stdout.splitlines() if line]

    def run_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._execute(*arguments)

    def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        self._execute("send-keys", "-t", pane_id, "-l", "--", text)
        if submit:
            self._execute("send-keys", "-t", pane_id, "Enter")

    def capture_pane(self, pane_id: str) -> bytes:
        argv = self._argv("capture-pane", "-p", "-e", "-S", "-", "-t", pane_id)
        result = subprocess.run(argv, capture_output=True, check=False)
        if result.returncode != 0:
            raise TmuxCommandError(argv, result.returncode)
        return result.stdout

    def attach_argv(self, session_name: str = "main") -> list[str]:
        return self._argv("attach-session", "-t", session_name)

    def kill_server(self) -> None:
        self._execute("kill-server", check=False)
