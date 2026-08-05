"""Subprocess-safe tmux lifecycle calls for one private server."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
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


_VERSION = re.compile(r"\btmux\s+(\d+)\.(\d+)[a-z]*\b", re.IGNORECASE)
_DIAGNOSTIC_LIMIT = 160


def _diagnostic_output(value: str | None) -> str:
    compact = " ".join((value or "").splitlines()).strip()
    if len(compact) > _DIAGNOSTIC_LIMIT:
        compact = f"{compact[: _DIAGNOSTIC_LIMIT - 3]}..."
    return repr(compact)


def tmux_subprocess_environment() -> dict[str, str]:
    """Prevent frozen private libraries from leaking into external tmux."""

    environment = os.environ.copy()
    if not getattr(sys, "frozen", False):
        return environment

    # PyInstaller's bootloader prepends its private directory to
    # ``LD_LIBRARY_PATH``.  Restoring ``LD_LIBRARY_PATH_ORIG`` is not safe:
    # depending on the launcher it can itself contain the private directory.
    # tmux is an external system executable, so let the dynamic linker resolve
    # its dependencies from the host and remove both bootstrap variables.
    environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_LIBRARY_PATH_ORIG", None)
    return environment


@dataclass(frozen=True, slots=True)
class TmuxSessionIdentity:
    session_id: str
    session_name: str


@dataclass(frozen=True, slots=True)
class TmuxClient:
    tty: str
    activity: int
    cols: int
    rows: int
    control_mode: bool
    termname: str


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
        env=tmux_subprocess_environment(),
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
        output = "\n".join((result.stdout or "", result.stderr or ""))
        match = _VERSION.search(output) if result.returncode == 0 else None
        if match is None:
            raise TmuxUnavailable(
                "Unable to determine tmux version "
                f"(exit={result.returncode}; "
                f"stdout={_diagnostic_output(result.stdout)}; "
                f"stderr={_diagnostic_output(result.stderr)})"
            )
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

    @staticmethod
    def _session_identity_line(line: str) -> TmuxSessionIdentity:
        session_id, separator, session_name = line.rstrip("\n").partition("\t")
        if (
            not separator
            or not session_id.startswith("$")
            or not session_id[1:].isdigit()
            or not session_name
        ):
            raise TmuxCommandError(["tmux", "session-identity"], 1)
        return TmuxSessionIdentity(session_id, session_name)

    def session_identity(self, target: str | None = None) -> TmuxSessionIdentity:
        format_string = "#{session_id}\t#{session_name}"
        if target is None:
            result = self._execute("list-sessions", "-F", format_string)
            lines = [line for line in result.stdout.splitlines() if line]
            if len(lines) != 1:
                raise TmuxCommandError(self._argv("list-sessions"), 1)
            return self._session_identity_line(lines[0])
        result = self._execute(
            "display-message",
            "-p",
            "-t",
            target,
            format_string,
        )
        return self._session_identity_line(result.stdout)

    def rename_session(self, target: str, name: str) -> None:
        self._execute("rename-session", "-t", target, name)

    def kill_session(self, target: str) -> None:
        self._execute("kill-session", "-t", target, check=False)

    def list_clients(self, target: str) -> list[TmuxClient]:
        format_string = "\t".join(
            (
                "#{client_tty}",
                "#{client_activity}",
                "#{client_width}",
                "#{client_height}",
                "#{client_control_mode}",
                "#{client_termname}",
            )
        )
        result = self._execute(
            "list-clients", "-t", target, "-F", format_string, check=False
        )
        if result.returncode != 0:
            return []
        clients: list[TmuxClient] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            try:
                activity = int(fields[1] or "0")
                cols = int(fields[2] or "0")
                rows = int(fields[3] or "0")
            except ValueError:
                continue
            clients.append(
                TmuxClient(
                    tty=fields[0],
                    activity=activity,
                    cols=cols,
                    rows=rows,
                    control_mode=fields[4] == "1",
                    termname=fields[5],
                )
            )
        return clients

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
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            env=tmux_subprocess_environment(),
        )
        if result.returncode != 0:
            raise TmuxCommandError(argv, result.returncode)
        return result.stdout

    def attach_argv(self, session_name: str = "main") -> list[str]:
        return self._argv("attach-session", "-t", session_name)

    def kill_server(self) -> None:
        self._execute("kill-server", check=False)
