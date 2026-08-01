"""A raw POSIX PTY attached to one stable tmux Session ID."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import struct
import termios
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

MAX_CHUNK_BYTES = 65_536
DEFAULT_RING_BYTES = 1024 * 1024


class ReplayGap(LookupError):
    pass


class ByteOutputRing:
    """A sequence-aware ring whose hard bound is encoded byte count."""

    def __init__(self, *, max_bytes: int = DEFAULT_RING_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._chunks: deque[tuple[int, bytes]] = deque()
        self._total_bytes = 0
        self._dropped_through = 0
        self._last_seq = 0

    def append(self, seq: int, data: bytes) -> None:
        if seq != self._last_seq + 1:
            raise ValueError("output sequences must be contiguous")
        if len(data) > self._max_bytes:
            raise ValueError("one output chunk exceeds ring capacity")
        self._last_seq = seq
        while self._chunks and self._total_bytes + len(data) > self._max_bytes:
            dropped_seq, dropped = self._chunks.popleft()
            self._total_bytes -= len(dropped)
            self._dropped_through = dropped_seq
        self._chunks.append((seq, bytes(data)))
        self._total_bytes += len(data)

    def replay_after(self, after_seq: int) -> list[tuple[int, bytes]]:
        if after_seq < self._dropped_through or after_seq > self._last_seq:
            raise ReplayGap(f"sequence {after_seq} is outside the retained output range")
        return [(seq, data) for seq, data in self._chunks if seq > after_seq]

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def first_seq(self) -> int | None:
        return self._chunks[0][0] if self._chunks else None

    @property
    def last_seq(self) -> int:
        return self._last_seq


@dataclass(frozen=True, slots=True)
class RemoteOutputChunk:
    terminal_id: UUID
    stream_id: UUID
    seq: int
    data: bytes


class PtyProcess(Protocol):
    master_fd: int
    slave_tty: str

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


class PtyAdapter(Protocol):
    async def spawn(
        self,
        socket_path: Path,
        session_id: str,
        rows: int,
        cols: int,
    ) -> PtyProcess: ...

    async def read(self, process: PtyProcess, max_bytes: int) -> bytes: ...

    async def write(self, process: PtyProcess, data: bytes) -> None: ...

    def resize(self, process: PtyProcess, rows: int, cols: int) -> None: ...

    def close_master(self, process: PtyProcess) -> None: ...


@dataclass(slots=True)
class PosixPtyProcess:
    master_fd: int
    slave_tty: str
    process: asyncio.subprocess.Process

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> int:
        return await self.process.wait()

    def terminate(self) -> None:
        self.process.terminate()


def _set_winsize(descriptor: int, rows: int, cols: int) -> None:
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PosixPtyAdapter:
    """Small asyncio adapter; it never invokes a shell or a tmux kill command."""

    async def spawn(
        self,
        socket_path: Path,
        session_id: str,
        rows: int,
        cols: int,
    ) -> PosixPtyProcess:
        master_fd, slave_fd = os.openpty()
        try:
            _set_winsize(slave_fd, rows, cols)
            slave_tty = os.ttyname(slave_fd)
            environment = os.environ.copy()
            environment.pop("TMUX", None)
            environment["TERMFLOW_PROXY_CLIENT"] = "1"
            environment["TERM"] = "xterm-256color"
            process = await asyncio.create_subprocess_exec(
                "tmux",
                "-S",
                str(socket_path),
                "attach-session",
                "-t",
                session_id,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return PosixPtyProcess(master_fd, slave_tty, process)

    async def read(self, process: PtyProcess, max_bytes: int) -> bytes:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[bytes] = loop.create_future()

        def readable() -> None:
            try:
                data = os.read(process.master_fd, max_bytes)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EIO:
                    data = b""
                else:
                    result.set_exception(exc)
                    loop.remove_reader(process.master_fd)
                    return
            loop.remove_reader(process.master_fd)
            if not result.done():
                result.set_result(data)

        loop.add_reader(process.master_fd, readable)
        try:
            return await result
        finally:
            loop.remove_reader(process.master_fd)

    async def write(self, process: PtyProcess, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        view = memoryview(data)
        while view:
            try:
                written = os.write(process.master_fd, view)
                view = view[written:]
            except BlockingIOError:
                writable = loop.create_future()
                loop.add_writer(process.master_fd, writable.set_result, None)
                try:
                    await writable
                finally:
                    loop.remove_writer(process.master_fd)

    def resize(self, process: PtyProcess, rows: int, cols: int) -> None:
        _set_winsize(process.master_fd, rows, cols)

    def close_master(self, process: PtyProcess) -> None:
        try:
            os.close(process.master_fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


OutputCallback = Callable[[RemoteOutputChunk], Awaitable[None]]
CloseCallback = Callable[[str], Awaitable[None]]


async def _noop_output(chunk: RemoteOutputChunk) -> None:
    del chunk


async def _noop_close(reason: str) -> None:
    del reason


class RemoteTmuxClient:
    def __init__(
        self,
        *,
        terminal_id: UUID,
        socket_path: Path,
        session_id: str,
        rows: int,
        cols: int,
        adapter: PtyAdapter | None = None,
        on_output: OutputCallback = _noop_output,
        on_closed: CloseCallback = _noop_close,
        ring_bytes: int = DEFAULT_RING_BYTES,
    ) -> None:
        self.terminal_id = terminal_id
        self.socket_path = socket_path
        self.session_id = session_id
        self.rows = rows
        self.cols = cols
        self.stream_id = uuid4()
        self.ring = ByteOutputRing(max_bytes=ring_bytes)
        self._adapter = adapter or PosixPtyAdapter()
        self._on_output = on_output
        self._on_closed = on_closed
        self._process: PtyProcess | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._seq = 0
        self._closing = False
        self._notified = False
        self._ready = asyncio.Event()

    @property
    def slave_tty(self) -> str | None:
        return self._process.slave_tty if self._process is not None else None

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def wait_ready(self, *, wait_seconds: float = 5.0) -> None:
        async with asyncio.timeout(wait_seconds):
            await self._ready.wait()

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await self._adapter.spawn(
            self.socket_path,
            self.session_id,
            self.rows,
            self.cols,
        )
        self._pump_task = asyncio.create_task(self._pump_output())

    async def _notify_closed(self, reason: str) -> None:
        if self._notified:
            return
        self._notified = True
        await self._on_closed(reason)

    async def _pump_output(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            while data := await self._adapter.read(process, MAX_CHUNK_BYTES):
                for offset in range(0, len(data), MAX_CHUNK_BYTES):
                    part = data[offset : offset + MAX_CHUNK_BYTES]
                    self._seq += 1
                    self.ring.append(self._seq, part)
                    self._ready.set()
                    await self._on_output(
                        RemoteOutputChunk(
                            terminal_id=self.terminal_id,
                            stream_id=self.stream_id,
                            seq=self._seq,
                            data=part,
                        )
                    )
            return_code = await process.wait()
            if not self._closing:
                await self._notify_closed(
                    "client_closed" if return_code == 0 else "internal_error"
                )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError):
            if not self._closing:
                await self._notify_closed("internal_error")

    async def write(self, data: bytes) -> None:
        if len(data) > MAX_CHUNK_BYTES:
            raise ValueError(f"terminal input exceeds {MAX_CHUNK_BYTES} bytes")
        process = self._process
        if process is None or self._closing:
            raise RuntimeError("remote tmux client is not open")
        await self._adapter.write(process, data)

    def resize(self, rows: int, cols: int) -> None:
        if rows < 1 or cols < 1:
            raise ValueError("terminal size must be positive")
        process = self._process
        if process is None or self._closing:
            raise RuntimeError("remote tmux client is not open")
        self.rows = rows
        self.cols = cols
        self._adapter.resize(process, rows, cols)

    def replay_after(self, seq: int) -> list[RemoteOutputChunk]:
        return [
            RemoteOutputChunk(self.terminal_id, self.stream_id, chunk_seq, data)
            for chunk_seq, data in self.ring.replay_after(seq)
        ]

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._closing = True
        pump_task = self._pump_task
        self._pump_task = None
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
        self._adapter.close_master(process)
        try:
            async with asyncio.timeout(1):
                await process.wait()
        except TimeoutError:
            process.terminate()
            await process.wait()
        self._process = None
        await self._notify_closed("client_closed")
