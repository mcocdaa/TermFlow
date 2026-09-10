from subprocess import CompletedProcess
from uuid import uuid4

import pytest
from termflow_node.tmux.actions import ActionRejected, TmuxActionExecutor
from termflow_protocol import (
    PaneSnapshot,
    TerminalActionPayload,
    TopologySnapshot,
    WindowSnapshot,
)


class ActionRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run_command(self, *arguments: str):
        self.calls.append(arguments)
        return CompletedProcess(list(arguments), 0, stdout="", stderr="")


def _topology(*, dead: bool = False) -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="actions",
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
                        dead=dead,
                    )
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("split_left_right", ("split-window", "-h", "-t", "%1")),
        ("split_top_bottom", ("split-window", "-v", "-t", "%1")),
        ("select_left", ("select-pane", "-t", "%1", "-L")),
        ("select_right", ("select-pane", "-t", "%1", "-R")),
        ("select_up", ("select-pane", "-t", "%1", "-U")),
        ("select_down", ("select-pane", "-t", "%1", "-D")),
        ("toggle_zoom", ("resize-pane", "-Z", "-t", "%1")),
        ("copy_mode", ("copy-mode", "-t", "%1")),
        ("close_pane", ("kill-pane", "-t", "%1")),
    ],
)
def test_pane_actions_map_to_direct_tmux_argv(action: str, expected: tuple[str, ...]) -> None:
    runner = ActionRunner()
    executor = TmuxActionExecutor(runner, "$0", topology_provider=_topology)
    executor.execute(
        TerminalActionPayload(
            terminal_id=uuid4(),
            action_id=uuid4(),
            action=action,
            target_pane_id="%1",
            confirmed=action == "close_pane",
        )
    )
    assert runner.calls == [expected]


def test_new_window_targets_stable_session_id() -> None:
    runner = ActionRunner()
    executor = TmuxActionExecutor(runner, "$0", topology_provider=_topology)
    executor.execute(
        TerminalActionPayload(
            terminal_id=uuid4(),
            action_id=uuid4(),
            action="new_window",
        )
    )
    assert runner.calls == [("new-window", "-t", "$0")]


@pytest.mark.parametrize("pane_id, dead", [("%9", False), ("%1", True)])
def test_pane_action_rejects_unknown_or_dead_target(pane_id: str, dead: bool) -> None:
    runner = ActionRunner()
    executor = TmuxActionExecutor(
        runner,
        "$0",
        topology_provider=lambda: _topology(dead=dead),
    )
    with pytest.raises(ActionRejected, match="pane_not_found"):
        executor.execute(
            TerminalActionPayload(
                terminal_id=uuid4(),
                action_id=uuid4(),
                action="toggle_zoom",
                target_pane_id=pane_id,
            )
        )
    assert runner.calls == []
