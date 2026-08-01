from uuid import uuid4

import pytest
from termflow_node.bridge.buffer import OutputBuffers
from termflow_node.bridge.input_handler import AsyncTmuxInput, InputHandler
from termflow_node.bridge.runtime import BridgeRuntime
from termflow_node.tmux.control_client import TmuxControlClient
from termflow_node.tmux.control_parser import OutputNotification
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader
from termflow_protocol import MessageType, WireMessage

pytestmark = pytest.mark.tmux


class CollectingTransport:
    def __init__(self) -> None:
        self.messages: list[WireMessage] = []

    def enqueue_nowait(self, message: WireMessage) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
async def test_runtime_buffers_real_tmux_output_without_owning_server_lifetime(tmp_path) -> None:
    socket_path = (tmp_path / "runtime.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "test")
    control = TmuxControlClient(socket_path, "main")
    await control.start()
    try:
        topology = TopologyReader(runner)
        current = topology.read()
        pane_id = current.windows[0].panes[0].pane_id
        buffers = OutputBuffers(max_bytes_per_pane=1024)
        transport = CollectingTransport()
        runtime = BridgeRuntime(
            instance_id=uuid4(),
            control=control,
            topology_provider=topology.read,
            transport=transport,
            buffers=buffers,
            input_handler=InputHandler(
                topology_provider=topology.read,
                sender=AsyncTmuxInput(runner),
            ),
        )
        await runtime.process_notification(OutputNotification(pane_id, b"real\xff"))
        assert buffers.total_bytes == 5
        assert transport.messages[-1].type is MessageType.PANE_OUTPUT
    finally:
        await control.close()
    assert runner.is_alive()
    runner.kill_server()
