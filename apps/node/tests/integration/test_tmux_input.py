import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_node.bridge.input_handler import AsyncTmuxInput, InputHandler
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader
from termflow_protocol import PaneInputPayload

pytestmark = pytest.mark.tmux


@pytest.mark.asyncio
async def test_literal_text_and_enter_reach_real_pane(tmp_path) -> None:
    socket_path = (tmp_path / "input.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "test")
    try:
        topology_reader = TopologyReader(runner)
        pane_id = topology_reader.read().windows[0].panes[0].pane_id
        runner.send_text(pane_id, "cat", True)
        handler = InputHandler(
            topology_provider=topology_reader.read,
            sender=AsyncTmuxInput(runner),
        )
        result = await handler.handle(
            PaneInputPayload(
                command_id=uuid4(),
                idempotency_key=uuid4(),
                pane_id=pane_id,
                text="hello-termflow",
                submit=True,
            )
        )
        assert result.ok is True
        await asyncio.sleep(0.05)
        captured = runner.capture_pane(pane_id)
        assert b"hello-termflow" in captured
    finally:
        runner.kill_server()


def test_control_character_is_rejected_before_tmux() -> None:
    with pytest.raises(ValidationError):
        PaneInputPayload(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            pane_id="%1",
            text="x\x03",
            submit=False,
        )
