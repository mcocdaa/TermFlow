import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from termflow_node.tmux.remote_client import (
    ByteOutputRing,
    PosixPtyAdapter,
    RemoteOutputChunk,
    RemoteTmuxClient,
    ReplayGap,
)


@pytest.mark.asyncio
async def test_posix_adapter_removes_inherited_tmux_environment(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class SpawnedProcess:
        returncode = None

    async def fake_subprocess(*args, **kwargs):
        del args
        environment = kwargs["env"]
        captured["has_tmux"] = "TMUX" in environment
        captured["proxy"] = environment.get("TERMFLOW_PROXY_CLIENT")
        captured["term"] = environment.get("TERM")
        return SpawnedProcess()

    monkeypatch.setenv("TMUX", "/tmp/parent.sock,123,0")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    adapter = PosixPtyAdapter()

    process = await adapter.spawn(tmp_path / "target.sock", "$0", 24, 80)

    try:
        assert captured == {
            "has_tmux": False,
            "proxy": "1",
            "term": "xterm-256color",
        }
    finally:
        adapter.close_master(process)


class FakeProcess:
    def __init__(self) -> None:
        self.master_fd = 9
        self.slave_tty = "/dev/pts/fake"
        self.returncode: int | None = None
        self.exited = asyncio.Event()
        self.terminated = False

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.exited.set()


class FakeAdapter:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.reads: asyncio.Queue[bytes] = asyncio.Queue()
        self.spawned: tuple[Path, str, int, int] | None = None
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False

    async def spawn(self, socket_path: Path, session_id: str, rows: int, cols: int):
        self.spawned = (socket_path, session_id, rows, cols)
        return self.process

    async def read(self, process: FakeProcess, max_bytes: int) -> bytes:
        return await self.reads.get()

    async def write(self, process: FakeProcess, data: bytes) -> None:
        self.writes.append(data)

    def resize(self, process: FakeProcess, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def close_master(self, process: FakeProcess) -> None:
        self.closed = True
        if process.returncode is None:
            process.returncode = 0
            process.exited.set()


@pytest.mark.asyncio
async def test_remote_client_sequences_raw_output_and_handles_binary_input(tmp_path) -> None:
    adapter = FakeAdapter()
    outputs: list[RemoteOutputChunk] = []
    closed: list[str] = []

    async def on_output(chunk: RemoteOutputChunk) -> None:
        outputs.append(chunk)

    async def on_closed(reason: str) -> None:
        closed.append(reason)

    terminal_id = uuid4()
    client = RemoteTmuxClient(
        terminal_id=terminal_id,
        socket_path=(tmp_path / "tmux.sock").absolute(),
        session_id="$4",
        rows=24,
        cols=80,
        adapter=adapter,
        on_output=on_output,
        on_closed=on_closed,
    )
    await client.start()
    await adapter.reads.put(b"\xff" + b"x" * 70_000)
    for _ in range(100):
        if len(outputs) == 2:
            break
        await asyncio.sleep(0)

    assert adapter.spawned == (client.socket_path, "$4", 24, 80)
    assert [chunk.seq for chunk in outputs] == [1, 2]
    assert all(chunk.terminal_id == terminal_id for chunk in outputs)
    assert len({chunk.stream_id for chunk in outputs}) == 1
    assert len(outputs[0].data) == 65_536
    assert b"".join(chunk.data for chunk in outputs) == b"\xff" + b"x" * 70_000

    await client.write(b"\x00\xff\x1b[31m")
    client.resize(40, 120)
    assert adapter.writes == [b"\x00\xff\x1b[31m"]
    assert adapter.resizes == [(40, 120)]

    await client.close()
    assert adapter.closed is True
    assert adapter.process.terminated is False
    assert closed == ["client_closed"]


@pytest.mark.asyncio
async def test_remote_client_reports_abnormal_tmux_client_exit(tmp_path) -> None:
    adapter = FakeAdapter()
    reasons: list[str] = []

    async def on_closed(reason: str) -> None:
        reasons.append(reason)

    client = RemoteTmuxClient(
        terminal_id=uuid4(),
        socket_path=(tmp_path / "tmux.sock").absolute(),
        session_id="$0",
        rows=24,
        cols=80,
        adapter=adapter,
        on_closed=on_closed,
    )
    await client.start()
    adapter.process.returncode = 1
    adapter.process.exited.set()
    await adapter.reads.put(b"")
    for _ in range(100):
        if reasons:
            break
        await asyncio.sleep(0)
    assert reasons == ["internal_error"]


@pytest.mark.asyncio
async def test_remote_client_ready_means_first_tmux_redraw_arrived(tmp_path) -> None:
    adapter = FakeAdapter()
    await adapter.reads.put(b"first redraw")
    client = RemoteTmuxClient(
        terminal_id=uuid4(),
        socket_path=(tmp_path / "tmux.sock").absolute(),
        session_id="$0",
        rows=24,
        cols=80,
        adapter=adapter,
    )
    await client.start()
    await client.wait_ready(wait_seconds=0.1)
    assert client.ready is True
    await client.close()


def test_byte_ring_replays_exact_sequences_and_detects_overwrite() -> None:
    ring = ByteOutputRing(max_bytes=5)
    ring.append(1, b"aa")
    ring.append(2, b"bbb")
    assert ring.replay_after(0) == [(1, b"aa"), (2, b"bbb")]
    ring.append(3, b"cc")
    assert ring.total_bytes == 5
    assert ring.replay_after(1) == [(2, b"bbb"), (3, b"cc")]
    with pytest.raises(ReplayGap):
        ring.replay_after(0)
    with pytest.raises(ReplayGap):
        ring.replay_after(4)


def test_byte_ring_is_bounded_by_bytes_not_chunk_count() -> None:
    ring = ByteOutputRing(max_bytes=1024 * 1024)
    for seq in range(1, 18):
        ring.append(seq, b"x" * 65_536)
    assert ring.total_bytes == 1024 * 1024
    assert ring.first_seq == 2
    assert ring.last_seq == 17
