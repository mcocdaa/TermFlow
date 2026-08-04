"""Async tmux control-mode process for notifications and capture."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from .control_parser import ControlNotification, parse_control_line
from .runner import tmux_subprocess_environment


class ControlClientNotStarted(RuntimeError):
    pass


class TmuxControlClient:
    def __init__(self, socket_path: Path, session_name: str) -> None:
        self._socket_path = socket_path
        self._session_name = session_name
        self._process: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            str(self._socket_path),
            "-C",
            "attach-session",
            "-t",
            self._session_name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=tmux_subprocess_environment(),
        )
        await self.write_command("refresh-client -f pause-after=5")

    def _running_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise ControlClientNotStarted("tmux control client is not running")
        return self._process

    async def write_command(self, command: str) -> None:
        process = self._running_process()
        if process.stdin is None:
            raise ControlClientNotStarted("tmux control stdin is unavailable")
        async with self._write_lock:
            process.stdin.write(command.encode("utf-8") + b"\n")
            await process.stdin.drain()

    async def notifications(self) -> AsyncIterator[ControlNotification]:
        process = self._running_process()
        if process.stdout is None:
            raise ControlClientNotStarted("tmux control stdout is unavailable")
        while line := await process.stdout.readline():
            yield parse_control_line(line)

    async def capture_pane(self, pane_id: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            str(self._socket_path),
            "capture-pane",
            "-p",
            "-e",
            "-S",
            "-",
            "-t",
            pane_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=tmux_subprocess_environment(),
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"tmux capture-pane failed with exit code {process.returncode}")
        return stdout

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            process.terminate()
            await process.wait()
