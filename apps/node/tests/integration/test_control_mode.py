import asyncio

import pytest
from termflow_node.tmux.control_client import TmuxControlClient
from termflow_node.tmux.control_parser import OutputNotification
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader

pytestmark = pytest.mark.tmux


@pytest.mark.asyncio
async def test_control_mode_observes_output_and_topology(tmp_path) -> None:
    socket_path = (tmp_path / "control.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "test")
    control = TmuxControlClient(socket_path, "main")
    await control.start()
    try:
        runner.run_command("split-window", "-t", "main")
        topology = TopologyReader(runner).read()
        assert sum(len(window.panes) for window in topology.windows) == 2
        pane_id = next(
            pane.pane_id for pane in topology.windows[0].panes if pane.active
        )
        marker = "TERMFLOW_CONTROL_MARKER"
        runner.run_command("send-keys", "-t", pane_id, f"printf {marker}", "Enter")

        async def find_output() -> OutputNotification:
            async for notification in control.notifications():
                if (
                    isinstance(notification, OutputNotification)
                    and marker.encode() in notification.data
                ):
                    return notification
            raise AssertionError("control mode ended before output")

        output = await asyncio.wait_for(find_output(), timeout=3)
        assert output.pane_id == pane_id
        captured = await control.capture_pane(pane_id)
        assert marker.encode() in captured
    finally:
        await control.close()
        runner.kill_server()
