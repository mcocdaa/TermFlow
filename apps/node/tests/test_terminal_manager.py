import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from termflow_node.bridge.terminal_manager import TerminalManager
from termflow_node.tmux.client_size import TerminalSize
from termflow_node.tmux.remote_client import ByteOutputRing, RemoteOutputChunk
from termflow_node.tmux.runner import TmuxClient
from termflow_protocol import (
    MessageType,
    PaneSnapshot,
    TerminalBindingsPayload,
    TerminalOpenPayload,
    TopologySnapshot,
    WindowSnapshot,
    WireMessage,
)


def _topology(name: str = "term") -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name=name,
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=23,
                        active=True,
                        dead=False,
                    )
                ],
            )
        ],
    )


class FakeRemote:
    def __init__(self, **kwargs) -> None:
        self.terminal_id: UUID = kwargs["terminal_id"]
        self.stream_id = uuid4()
        self.slave_tty = "/dev/pts/fake"
        self.ring = ByteOutputRing(max_bytes=1024 * 1024)
        self._on_output = kwargs["on_output"]
        self._on_closed = kwargs["on_closed"]
        self.started = False
        self.closed = False
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._seq = 0
        self.emit_on_start: bytes | None = None

    async def start(self) -> None:
        self.started = True
        if self.emit_on_start is not None:
            await self.emit(self.emit_on_start)

    async def wait_ready(self, *, wait_seconds: float = 5.0) -> None:
        assert wait_seconds > 0

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    async def close(self) -> None:
        self.closed = True

    def replay_after(self, seq: int) -> list[RemoteOutputChunk]:
        return [
            RemoteOutputChunk(self.terminal_id, self.stream_id, chunk_seq, data)
            for chunk_seq, data in self.ring.replay_after(seq)
        ]

    async def emit(self, data: bytes) -> None:
        self._seq += 1
        self.ring.append(self._seq, data)
        await self._on_output(
            RemoteOutputChunk(self.terminal_id, self.stream_id, self._seq, data)
        )

    async def abnormal_exit(self) -> None:
        await self._on_closed("internal_error")


class FakeRunner:
    def __init__(self) -> None:
        self.clients = [
            TmuxClient(
                tty="/dev/pts/fake",
                activity=1,
                cols=80,
                rows=24,
                control_mode=False,
                termname="xterm-256color",
            )
        ]

    def list_clients(self, session_id: str) -> list[TmuxClient]:
        assert session_id == "$0"
        return self.clients


class FakeActions:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def execute(self, action) -> None:
        self.actions.append(action.action)


class FakeBindings:
    def read(self, terminal_id: UUID) -> TerminalBindingsPayload:
        return TerminalBindingsPayload(
            terminal_id=terminal_id,
            prefix="C-b",
            prefix2=None,
            bindings=[],
        )


class FakeRenamer:
    def __init__(self) -> None:
        self.names: list[str] = []

    def rename(self, name: str) -> TopologySnapshot:
        self.names.append(name)
        return _topology(name)


class Subject:
    def __init__(
        self,
        *,
        grace_seconds: float = 30.0,
        emit_on_start: bytes | None = None,
    ) -> None:
        self.instance_id = uuid4()
        self.messages: list[WireMessage] = []
        self.remotes: list[FakeRemote] = []
        self.actions = FakeActions()
        self.renamer = FakeRenamer()

        def remote_factory(**kwargs):
            remote = FakeRemote(**kwargs)
            remote.emit_on_start = emit_on_start
            self.remotes.append(remote)
            return remote

        self.manager = TerminalManager(
            instance_id=self.instance_id,
            socket_path=Path("/tmp/termflow-test.sock"),
            session_id="$0",
            runner=FakeRunner(),
            topology_provider=_topology,
            publish=lambda message: self.messages.append(message) or True,
            remote_factory=remote_factory,
            action_executor=self.actions,
            binding_reader=FakeBindings(),
            renamer=self.renamer,
            creation_size=TerminalSize(24, 80),
            grace_seconds=grace_seconds,
            size_poll_seconds=3600,
        )

    async def send(self, message_type: MessageType, payload: dict[str, object]) -> None:
        await self.manager.handle_wire_message(
            WireMessage(
                type=message_type,
                instance_id=self.instance_id,
                payload=payload,
            )
        )

    async def open(
        self,
        terminal_id: UUID,
        *,
        resume_stream_id: UUID | None = None,
        after_seq: int | None = None,
    ) -> None:
        await self.send(
            MessageType.TERMINAL_OPEN,
            TerminalOpenPayload(
                terminal_id=terminal_id,
                resume_stream_id=resume_stream_id,
                after_seq=after_seq,
            ).model_dump(mode="json"),
        )


@pytest.mark.asyncio
async def test_startup_output_is_released_only_after_open_metadata() -> None:
    subject = Subject(emit_on_start=b"first redraw")

    await subject.open(uuid4())

    assert [message.type for message in subject.messages] == [
        MessageType.TERMINAL_OPENED,
        MessageType.TERMINAL_BINDINGS,
        MessageType.TERMINAL_OUTPUT,
    ]
    assert subject.messages[-1].payload["seq"] == 1
    await subject.manager.close()


@pytest.mark.asyncio
async def test_open_output_input_action_and_close_are_multiplexed() -> None:
    subject = Subject()
    terminal_id = uuid4()
    await subject.open(terminal_id)
    remote = subject.remotes[0]
    assert remote.started
    assert [message.type for message in subject.messages[:2]] == [
        MessageType.TERMINAL_OPENED,
        MessageType.TERMINAL_BINDINGS,
    ]

    await remote.emit(b"\x00\xffscreen")
    assert subject.messages[-1].type is MessageType.TERMINAL_OUTPUT
    await subject.send(
        MessageType.TERMINAL_INPUT,
        {"terminal_id": terminal_id, "data_base64": "AP8="},
    )
    assert remote.writes == [b"\x00\xff"]

    action_id = uuid4()
    await subject.send(
        MessageType.TERMINAL_ACTION,
        {
            "terminal_id": terminal_id,
            "action_id": action_id,
            "action": "toggle_zoom",
            "target_pane_id": "%0",
            "confirmed": False,
        },
    )
    assert subject.actions.actions == ["toggle_zoom"]
    assert subject.messages[-2].type is MessageType.TERMINAL_ACTION_RESULT
    assert subject.messages[-1].type is MessageType.TOPOLOGY_CHANGED

    await subject.send(
        MessageType.TERMINAL_CLOSE,
        {"terminal_id": terminal_id, "reason": "client_closed"},
    )
    assert remote.closed
    assert subject.messages[-1].type is MessageType.TERMINAL_CLOSED
    await subject.manager.close()


@pytest.mark.asyncio
async def test_new_owner_deterministically_replaces_old_terminal() -> None:
    subject = Subject()
    first, second = uuid4(), uuid4()
    await subject.open(first)
    old = subject.remotes[0]
    subject.messages.clear()
    await subject.open(second)
    assert old.closed
    assert subject.messages[0].type is MessageType.TERMINAL_CLOSED
    assert subject.messages[0].payload["terminal_id"] == str(first)
    assert subject.messages[0].payload["reason"] == "replaced"
    assert len(subject.remotes) == 2
    await subject.manager.close()


@pytest.mark.asyncio
async def test_bridge_reconnect_replays_only_exact_terminal_stream_sequence() -> None:
    subject = Subject(grace_seconds=0.2)
    terminal_id = uuid4()
    await subject.open(terminal_id)
    remote = subject.remotes[0]
    await remote.emit(b"one")
    subject.messages.clear()
    subject.manager.bridge_disconnected()
    await remote.emit(b"two")
    assert subject.messages == []

    subject.manager.bridge_connected()
    await subject.open(
        terminal_id,
        resume_stream_id=remote.stream_id,
        after_seq=1,
    )
    assert len(subject.remotes) == 1
    assert [message.type for message in subject.messages] == [
        MessageType.TERMINAL_OPENED,
        MessageType.TERMINAL_BINDINGS,
        MessageType.TERMINAL_OUTPUT,
    ]
    assert subject.messages[-1].payload["seq"] == 2
    await subject.manager.close()


@pytest.mark.asyncio
async def test_stream_mismatch_closes_old_and_creates_fresh_client() -> None:
    subject = Subject()
    terminal_id = uuid4()
    await subject.open(terminal_id)
    old = subject.remotes[0]
    subject.messages.clear()
    await subject.open(terminal_id, resume_stream_id=uuid4(), after_seq=0)
    assert old.closed
    assert len(subject.remotes) == 2
    assert subject.messages[0].payload["reason"] == "stream_gap"
    assert subject.messages[-2].type is MessageType.TERMINAL_OPENED
    await subject.manager.close()


@pytest.mark.asyncio
async def test_grace_expiry_closes_retained_proxy() -> None:
    subject = Subject(grace_seconds=0.01)
    terminal_id = uuid4()
    await subject.open(terminal_id)
    subject.messages.clear()
    subject.manager.bridge_disconnected()
    await asyncio.sleep(0.03)
    assert subject.remotes[0].closed
    # Disconnected output is intentionally not queued; the next owner gets a fresh redraw.
    subject.manager.bridge_connected()
    assert subject.manager.current_terminal_id is None
    await subject.manager.close()


@pytest.mark.asyncio
async def test_rename_and_malformed_terminal_message_return_structured_results() -> None:
    subject = Subject()
    command_id = uuid4()
    await subject.send(
        MessageType.TERM_RENAME,
        {"command_id": command_id, "name": "renamed"},
    )
    assert subject.renamer.names == ["renamed"]
    assert subject.messages[-2].type is MessageType.TERM_RENAME_RESULT
    assert subject.messages[-1].type is MessageType.TOPOLOGY_CHANGED

    terminal_id = uuid4()
    await subject.manager.handle_wire_message(
        WireMessage(
            type=MessageType.TERMINAL_INPUT,
            instance_id=subject.instance_id,
            payload={"terminal_id": str(terminal_id), "data_base64": "not-base64"},
        )
    )
    assert subject.messages[-1].type is MessageType.TERMINAL_CLOSED
    assert subject.messages[-1].payload["error_code"] == "invalid_terminal_message"
    await subject.manager.close()
