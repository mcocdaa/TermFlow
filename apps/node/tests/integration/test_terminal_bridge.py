import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from termflow_node.bridge.terminal_manager import TerminalManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.actions import TermRenamer, TmuxActionExecutor
from termflow_node.tmux.bindings import TmuxBindingReader
from termflow_node.tmux.client_size import TerminalSize
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader
from termflow_protocol import (
    MessageType,
    TerminalActionPayload,
    TerminalClosePayload,
    TerminalInputPayload,
    TerminalOpenPayload,
    TerminalOutputPayload,
    WireMessage,
)

pytestmark = pytest.mark.tmux


@pytest.mark.asyncio
async def test_terminal_protocol_drives_real_pty_actions_and_safe_detach(tmp_path) -> None:
    socket_path = (tmp_path / "bridge-terminal.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("bridge-terminal", "main")
    identity = runner.session_identity()
    topology = TopologyReader(runner, identity.session_id)
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    store.save(
        LocalInstance(
            schema_version=2,
            instance_id=instance_id,
            name=identity.session_name,
            session_id=identity.session_id,
            session_name=identity.session_name,
            socket_path=socket_path,
            created_at=datetime.now(UTC),
            lifecycle=InstanceLifecycle.RUNNING,
        )
    )
    messages: list[WireMessage] = []
    manager = TerminalManager(
        instance_id=instance_id,
        socket_path=socket_path,
        session_id=identity.session_id,
        runner=runner,
        topology_provider=topology.read,
        publish=lambda message: messages.append(message) or True,
        action_executor=TmuxActionExecutor(
            runner,
            identity.session_id,
            topology_provider=topology.read,
        ),
        binding_reader=TmuxBindingReader(runner, identity.session_id),
        renamer=TermRenamer(
            runner=runner,
            store=store,
            instance_id=instance_id,
            topology_provider=topology.read,
        ),
        creation_size=TerminalSize(24, 80),
        size_poll_seconds=0.05,
    )
    terminal_id = uuid4()

    async def send(message_type: MessageType, payload: dict[str, object]) -> None:
        await manager.handle_wire_message(
            WireMessage(type=message_type, instance_id=instance_id, payload=payload)
        )

    try:
        await send(
            MessageType.TERMINAL_OPEN,
            TerminalOpenPayload(terminal_id=terminal_id).model_dump(mode="json"),
        )
        assert messages[0].type is MessageType.TERMINAL_OPENED
        await send(
            MessageType.TERMINAL_INPUT,
            TerminalInputPayload.from_bytes(
                terminal_id,
                b"printf TERMFLOW_BRIDGE_TERMINAL\r",
            ).model_dump(mode="json"),
        )
        output = b""
        for _ in range(100):
            output = b"".join(
                TerminalOutputPayload.model_validate(message.payload).to_bytes()
                for message in messages
                if message.type is MessageType.TERMINAL_OUTPUT
            )
            if b"TERMFLOW_BRIDGE_TERMINAL" in output:
                break
            await asyncio.sleep(0.01)
        assert b"TERMFLOW_BRIDGE_TERMINAL" in output

        pane_id = topology.read().windows[0].panes[0].pane_id
        await send(
            MessageType.TERMINAL_ACTION,
            TerminalActionPayload(
                terminal_id=terminal_id,
                action_id=uuid4(),
                action="split_left_right",
                target_pane_id=pane_id,
            ).model_dump(mode="json"),
        )
        assert len(topology.read().windows[0].panes) == 2
        assert any(
            message.type is MessageType.TERMINAL_ACTION_RESULT
            and message.payload["ok"] is True
            for message in messages
        )

        await send(
            MessageType.TERMINAL_CLOSE,
            TerminalClosePayload(
                terminal_id=terminal_id,
                reason="client_closed",
            ).model_dump(mode="json"),
        )
        assert runner.is_alive(identity.session_id)
        assert len(runner.list_pane_ids()) == 2
    finally:
        await manager.close()
        runner.kill_server()
