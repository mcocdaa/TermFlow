from uuid import UUID, uuid4

import pytest
from termflow_node.bridge.buffer import OutputBuffers
from termflow_node.bridge.runtime import BridgeRuntime
from termflow_node.tmux.capture import RenderedCapture
from termflow_node.tmux.control_parser import OutputNotification, PauseNotification
from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    PaneInputPayload,
    PaneKeyInputPayload,
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
    async def capture_pane_bounded(
        self,
        pane_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        tail_lines: int | None = None,
        join_wrapped: bool = False,
        full_history: bool = False,
        max_bytes: int,
    ) -> RenderedCapture:
        assert pane_id == "%1"
        return RenderedCapture(content=b"screen snapshot", truncated=False)


class FakeInput:
    def retain_panes(self, pane_ids: set[str]) -> None:
        pass


class DropOutputTransport(FakeTransport):
    """Fake transport whose wire queue is full for PANE_OUTPUT on demand.

    Control messages (STREAM_GAP) still flow, mirroring the reserved control
    slots in the real bridge queue.
    """

    def __init__(self, buffers: OutputBuffers) -> None:
        super().__init__(buffers)
        self.drop_output = False

    def enqueue_nowait(self, message: WireMessage) -> bool:
        if self.drop_output and message.type is MessageType.PANE_OUTPUT:
            return False
        return super().enqueue_nowait(message)


class FullTransport(FakeTransport):
    """Fake transport whose wire queue is entirely full."""

    def enqueue_nowait(self, message: WireMessage) -> bool:
        return False


class FakeTerminalManager:
    def __init__(self) -> None:
        self.messages: list[WireMessage] = []

    async def handle_wire_message(self, message: WireMessage) -> None:
        self.messages.append(message)

    async def close(self) -> None:
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
async def test_unavailable_replay_publishes_gap_and_resets_stream_without_snapshot() -> None:
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
    buffers.append("%1", b"live output")
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
    # A rendered snapshot must not be appended into the live output ring as
    # though it were new terminal output (§9.2): only the gap is published and
    # the ring is reset to a fresh stream.
    assert [message.type for message in transport.messages] == [MessageType.STREAM_GAP]
    buffer = buffers.for_pane("%1")
    assert buffer.total_bytes == 0
    assert buffer.last_seq == 0


@pytest.mark.asyncio
async def test_pause_publishes_gap_without_appending_snapshot_to_ring() -> None:
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
    await runtime.process_notification(OutputNotification("%1", b"live"))
    await runtime.process_notification(PauseNotification("%1", True))
    assert [message.type for message in transport.messages] == [
        MessageType.PANE_OUTPUT,
        MessageType.STREAM_GAP,
    ]
    assert buffers.for_pane("%1").total_bytes == 0
    assert buffers.for_pane("%1").last_seq == 0


@pytest.mark.asyncio
async def test_dropped_output_publishes_backpressure_gap_and_resets_ring() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = DropOutputTransport(buffers)
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    await runtime.process_notification(OutputNotification("%1", b"live"))
    transport.drop_output = True
    dropped = buffers.for_pane("%1").stream_id
    await runtime.process_notification(OutputNotification("%1", b"overflow"))
    # The drop is announced explicitly as a backpressure gap, never silently:
    # the peer reconciles through a bounded gap snapshot against the fresh
    # stream (§9.2).
    assert [message.type for message in transport.messages] == [
        MessageType.PANE_OUTPUT,
        MessageType.STREAM_GAP,
    ]
    gap = transport.messages[-1]
    assert gap.payload["reason"] == "backpressure"
    assert gap.payload["previous_stream_id"] == str(dropped)
    assert gap.payload["pane_id"] == "%1"
    ring = buffers.for_pane("%1")
    assert ring.stream_id != dropped
    assert ring.total_bytes == 0
    assert ring.last_seq == 0
    assert buffers.pane_incarnation("%1") == 2


@pytest.mark.asyncio
async def test_dropped_output_keeps_ring_when_gap_cannot_be_published() -> None:
    # When even the gap cannot be enqueued (queue fully stalled), the ring is
    # preserved instead of being reset silently: the peer still catches up
    # through the ring (seqs keep advancing; replay serves the chunk).
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = FullTransport(buffers)
    runtime = BridgeRuntime(
        instance_id=uuid4(),
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    await runtime.process_notification(OutputNotification("%1", b"overflow"))
    assert transport.messages == []
    ring = buffers.for_pane("%1")
    assert ring.total_bytes == 8
    assert ring.last_seq == 1
    assert buffers.pane_incarnation("%1") == 1


@pytest.mark.asyncio
async def test_replay_drop_publishes_backpressure_gap_and_resets_ring() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = DropOutputTransport(buffers)
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
    )
    first = buffers.append("%1", b"one")
    buffers.append("%1", b"two")
    transport.drop_output = True
    request = PaneReplayRequestPayload(
        pane_id="%1",
        stream_id=first.stream_id,
        after_seq=0,
    )
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_REPLAY_REQUEST,
            instance_id=instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    assert [message.type for message in transport.messages] == [MessageType.STREAM_GAP]
    gap = transport.messages[-1]
    assert gap.payload["reason"] == "backpressure"
    assert buffers.for_pane("%1").total_bytes == 0


@pytest.mark.asyncio
async def test_runtime_delegates_terminal_messages_without_disturbing_pane_channel() -> None:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = FakeTransport(buffers)
    terminals = FakeTerminalManager()
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=FakeInput(),
        terminal_manager=terminals,
    )
    message = WireMessage(
        type=MessageType.TERMINAL_OPEN,
        instance_id=instance_id,
        payload={"terminal_id": str(uuid4())},
    )
    await runtime.handle_message(message)
    assert terminals.messages == [message]


class RecordingInput:
    """Records dispatched input commands and answers with ok receipts."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, object]] = []

    async def handle(self, command: PaneInputPayload) -> CommandResultPayload:
        self.commands.append(("text", command))
        return CommandResultPayload(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            ok=True,
        )

    async def handle_keys(self, command: PaneKeyInputPayload) -> CommandResultPayload:
        self.commands.append(("keys", command))
        return CommandResultPayload(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            ok=True,
        )

    def retain_panes(self, pane_ids: set[str]) -> None:
        pass


def _runtime_with(handler) -> tuple[BridgeRuntime, OutputBuffers, FakeTransport, UUID]:
    buffers = OutputBuffers(max_bytes_per_pane=1024)
    transport = FakeTransport(buffers)
    instance_id = uuid4()
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=FakeControl(),
        topology_provider=topology,
        transport=transport,
        buffers=buffers,
        input_handler=handler,
    )
    return runtime, buffers, transport, instance_id


@pytest.mark.asyncio
async def test_pane_key_input_dispatches_to_handler_and_returns_receipt() -> None:
    handler = RecordingInput()
    runtime, _, transport, instance_id = _runtime_with(handler)
    command_id = uuid4()
    request = PaneKeyInputPayload(
        command_id=command_id,
        idempotency_key=uuid4(),
        pane_id="%1",
        keys=("ctrl-c",),
        pane_incarnation=1,
    )
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_KEY_INPUT,
            instance_id=instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    assert [message.type for message in transport.messages] == [MessageType.COMMAND_RESULT]
    assert transport.messages[0].payload["command_id"] == str(command_id)
    assert transport.messages[0].payload["ok"] is True
    assert handler.commands == [("keys", request)]


@pytest.mark.asyncio
async def test_pane_input_with_stale_incarnation_is_rejected() -> None:
    handler = RecordingInput()
    runtime, _, transport, instance_id = _runtime_with(handler)
    command_id = uuid4()
    request = PaneInputPayload(
        command_id=command_id,
        idempotency_key=uuid4(),
        pane_id="%1",
        text="ls",
        submit=True,
        pane_incarnation=99,  # the ledger incarnation no longer matches
    )
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_INPUT,
            instance_id=instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    result = transport.messages[0]
    assert result.payload["ok"] is False
    assert result.payload["error_code"] == "incarnation_changed"
    assert handler.commands == []


@pytest.mark.asyncio
async def test_pane_input_without_incarnation_skips_the_check() -> None:
    handler = RecordingInput()
    runtime, _, transport, instance_id = _runtime_with(handler)
    request = PaneInputPayload(
        command_id=uuid4(),
        idempotency_key=uuid4(),
        pane_id="%1",
        text="ls",
        submit=True,
    )
    await runtime.handle_message(
        WireMessage(
            type=MessageType.PANE_INPUT,
            instance_id=instance_id,
            payload=request.model_dump(mode="json"),
        )
    )
    assert transport.messages[0].payload["ok"] is True
    # The C terminal input path keeps its existing behavior: the handler
    # still runs its pane existence check.
    assert handler.commands and handler.commands[0][0] == "text"
