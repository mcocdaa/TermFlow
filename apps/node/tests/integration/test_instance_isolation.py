import asyncio
from uuid import UUID, uuid4

import pytest
from termflow_node.bridge.buffer import OutputBuffers
from termflow_node.bridge.runtime import BridgeRuntime
from termflow_node.tmux.control_parser import OutputNotification
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader
from termflow_protocol import MessageType, PaneOutputPayload, WireMessage

pytestmark = pytest.mark.tmux


class CollectingTransport:
    def __init__(self) -> None:
        self.messages: list[WireMessage] = []

    def enqueue_nowait(self, message: WireMessage) -> bool:
        self.messages.append(message)
        return True


class CaptureControl:
    def __init__(self, runner: TmuxRunner) -> None:
        self.runner = runner

    async def capture_pane(self, pane_id: str) -> bytes:
        return self.runner.capture_pane(pane_id)


class NoopInput:
    def retain_panes(self, pane_ids: set[str]) -> None:
        pass


@pytest.mark.asyncio
async def test_two_private_servers_remain_independent(tmp_path) -> None:
    runners = [
        TmuxRunner((tmp_path / name / "tmux.sock").absolute())
        for name in ("first", "second")
    ]
    for runner in runners:
        runner.socket_path.parent.mkdir(mode=0o700)
        runner.create_session("main", "test")
    try:
        instance_ids = [uuid4(), uuid4()]
        sentinels = [b"INSTANCE_ALPHA", b"INSTANCE_BETA"]
        transports = [CollectingTransport(), CollectingTransport()]
        for runner, instance_id, sentinel, transport in zip(
            runners,
            instance_ids,
            sentinels,
            transports,
            strict=True,
        ):
            topology_reader = TopologyReader(runner)
            pane_id = topology_reader.read().windows[0].panes[0].pane_id
            runner.send_text(pane_id, f"printf {sentinel.decode()}", True)
            await asyncio.sleep(0.03)
            screen = runner.capture_pane(pane_id)
            runtime = BridgeRuntime(
                instance_id=instance_id,
                control=CaptureControl(runner),
                topology_provider=topology_reader.read,
                transport=transport,
                buffers=OutputBuffers(max_bytes_per_pane=4096),
                input_handler=NoopInput(),
            )
            await runtime.process_notification(OutputNotification(pane_id, screen))

        for index, transport in enumerate(transports):
            output = next(
                message for message in transport.messages if message.type is MessageType.PANE_OUTPUT
            )
            assert UUID(str(output.instance_id)) == instance_ids[index]
            body = PaneOutputPayload.model_validate(output.payload).to_bytes()
            assert sentinels[index] in body
            assert sentinels[1 - index] not in body

        runners[0].kill_server()
        assert not runners[0].is_alive()
        assert runners[1].is_alive()
    finally:
        for runner in runners:
            runner.kill_server()
