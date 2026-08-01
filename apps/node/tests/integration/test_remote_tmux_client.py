import asyncio
from uuid import uuid4

import pytest
from termflow_node.tmux.remote_client import RemoteOutputChunk, RemoteTmuxClient
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader

pytestmark = pytest.mark.tmux


@pytest.mark.asyncio
async def test_remote_pty_drives_real_tmux_and_detaches_without_killing_session(tmp_path) -> None:
    socket_path = (tmp_path / "remote.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("remote-term", "main")
    identity = runner.session_identity()
    runner.run_command("set-option", "-t", identity.session_id, "prefix", "C-b")
    output = bytearray()
    output_ready = asyncio.Event()

    async def on_output(chunk: RemoteOutputChunk) -> None:
        output.extend(chunk.data)
        output_ready.set()

    client = RemoteTmuxClient(
        terminal_id=uuid4(),
        socket_path=socket_path,
        session_id=identity.session_id,
        rows=24,
        cols=80,
        on_output=on_output,
    )
    try:
        await client.start()
        await client.wait_ready(wait_seconds=3)
        assert output
        for _ in range(100):
            if any(
                attached.tty == client.slave_tty
                for attached in runner.list_clients(identity.session_id)
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("remote tmux client did not attach")

        await client.write(b"\x02c")
        for _ in range(100):
            topology = TopologyReader(runner, identity.session_id).read()
            if len(topology.windows) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(topology.windows) == 2

        await client.write(b"printf TERMFLOW_REMOTE_MARKER\r")
        for _ in range(100):
            if b"TERMFLOW_REMOTE_MARKER" in output:
                break
            output_ready.clear()
            await asyncio.wait_for(output_ready.wait(), timeout=1)
        assert b"TERMFLOW_REMOTE_MARKER" in output
        await client.close()
        assert runner.is_alive(identity.session_id)
        assert runner.list_pane_ids()
    finally:
        await client.close()
        runner.kill_server()
