from datetime import UTC, datetime
from uuid import uuid4

import pytest
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.actions import TermRenamer, TmuxActionExecutor
from termflow_node.tmux.bindings import TmuxBindingReader
from termflow_node.tmux.runner import TmuxRunner
from termflow_node.tmux.topology import TopologyReader
from termflow_protocol import TerminalActionPayload

pytestmark = pytest.mark.tmux


def _action(action: str, pane_id: str | None = None) -> TerminalActionPayload:
    return TerminalActionPayload(
        terminal_id=uuid4(),
        action_id=uuid4(),
        action=action,
        target_pane_id=pane_id,
        confirmed=action == "close_pane",
    )


def test_real_tmux_actions_bindings_and_rename_use_stable_ids(tmp_path) -> None:
    socket_path = (tmp_path / "actions.sock").absolute()
    runner = TmuxRunner(socket_path)
    runner.create_session("actions", "main")
    identity = runner.session_identity()
    reader = TopologyReader(runner, identity.session_id)
    executor = TmuxActionExecutor(runner, identity.session_id, topology_provider=reader.read)
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    store.save(
        LocalInstance(
            schema_version=2,
            instance_id=instance_id,
            name="actions",
            session_id=identity.session_id,
            session_name="actions",
            socket_path=socket_path,
            created_at=datetime.now(UTC),
            lifecycle=InstanceLifecycle.RUNNING,
        )
    )
    try:
        first = reader.read().windows[0].panes[0].pane_id
        executor.execute(_action("split_left_right", first))
        panes = reader.read().windows[0].panes
        assert len(panes) == 2
        left, right = sorted(panes, key=lambda pane: pane.left)

        executor.execute(_action("select_left", right.pane_id))
        assert next(pane for pane in reader.read().windows[0].panes if pane.active).pane_id == (
            left.pane_id
        )
        executor.execute(_action("split_top_bottom", left.pane_id))
        assert len(reader.read().windows[0].panes) == 3

        active = next(pane for pane in reader.read().windows[0].panes if pane.active)
        executor.execute(_action("toggle_zoom", active.pane_id))
        zoomed = runner.run_command(
            "display-message", "-p", "-t", active.pane_id, "#{window_zoomed_flag}"
        ).stdout.strip()
        assert zoomed == "1"
        executor.execute(_action("toggle_zoom", active.pane_id))

        executor.execute(_action("copy_mode", active.pane_id))
        mode = runner.run_command(
            "display-message", "-p", "-t", active.pane_id, "#{pane_mode}"
        ).stdout.strip()
        assert "copy-mode" in mode

        executor.execute(_action("new_window"))
        assert len(reader.read().windows) == 2
        executor.execute(_action("close_pane", active.pane_id))
        assert all(
            pane.pane_id != active.pane_id
            for window in reader.read().windows
            for pane in window.panes
        )

        bindings = TmuxBindingReader(runner, identity.session_id).read(uuid4())
        assert bindings.prefix
        assert len(bindings.bindings) == 10

        renamed_topology = TermRenamer(
            runner=runner,
            store=store,
            instance_id=instance_id,
            topology_provider=reader.read,
        ).rename("renamed actions")
        refreshed = store.load(instance_id)
        assert refreshed.session_id == identity.session_id
        assert refreshed.session_name == "renamed actions"
        assert renamed_topology.session_name == "renamed actions"
        assert runner.is_alive(identity.session_id)
    finally:
        runner.kill_server()
