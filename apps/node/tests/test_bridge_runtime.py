from uuid import uuid4

import pytest
from termflow_node.bridge.buffer import OutputBuffers
from termflow_node.bridge.runtime import BridgeRuntime
from termflow_node.tmux.control_parser import OutputNotification
from termflow_protocol import (
    MessageType,
    PaneOutputPayload,
    PaneReplayRequestPayload,
    PaneSnapshot,
    TopologySnapshot,
    WindowSnapshot,
    WireMessage,
)


def topology() -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    )
                ],
            )
        ],
    )


class FakeTransport:
    def __init__(self, buffers: OutputBuffers) -> None:
        self.messages: list[WireMessage] = []
        self.buffers = buffers

    def enqueue_nowait(self, message: WireMessage) -> bool:
        if message.type is MessageType.PANE_OUTPUT:
            assert self.buffers.total_bytes > 0
        self.messages.append(message)
        return True


class FakeControl:
    async def capture_pane(self, pane_id: str) -> bytes:
        assert pane_id == "%1"
        return b"screen snapshot"


class FakeInput:
    def retain_panes(self, pane_ids: set[str]) -> None:
        pass


@pytest.mark.asyncio
async def test_tmux_output_is_buffered_before_network_publish() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = FakeTransport(buffers)
    runtime = BridgeRuntime(
        instance_id=uuid4(),
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    await runtime.process_notification(OutputNotification("%1", b"hello\xff"))
    event = transport.messages[-1]
    assert event.type is MessageType.PANE_OUTPUT
    assert event.payload["data_base64"] == "aGVsbG//"
    assert buffers.for_pane("%1").total_bytes == 6


@pytest.mark.asyncio
async def test_unavailable_replay_sends_gap_then_capture_snapshot() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = FakeTransport(buffers)
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    request = PaneReplayRequestPayload(
        pane_id="%1",
        stream_id=uuid4(),
        after_seq=4,
    )
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_REPLAY_REQUEST,
            instance_id=instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    assert [message.type for message in transport.messages] == [
        MessageType.STREAM_GAP,
        MessageType.PANE_OUTPUT,
    ]
    output = PaneOutputPayload.model_validate(transport.messages[-1].payload)
    assert output.to_bytes() == b"screen snapshot"
