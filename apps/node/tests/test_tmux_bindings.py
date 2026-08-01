from subprocess import CompletedProcess
from uuid import uuid4

from termflow_node.tmux.bindings import TmuxBindingReader


class BindingRunner:
    def run_command(self, *arguments: str):
        if arguments[:2] == ("show-options", "-gv"):
            option = arguments[-1]
            value = "C-a\n" if option == "prefix" else "None\n"
            return CompletedProcess(list(arguments), 0, stdout=value, stderr="")
        assert arguments == ("list-keys", "-T", "prefix")
        return CompletedProcess(
            list(arguments),
            0,
            stdout=(
                "bind-key -T prefix | split-window -h\n"
                "bind-key -T prefix - split-window -v\n"
                "bind-key -T prefix c new-window\n"
                "bind-key -T prefix h select-pane -L\n"
                "bind-key -T prefix z resize-pane -Z\n"
            ),
            stderr="",
        )


def test_binding_snapshot_uses_live_prefix_and_detected_keys() -> None:
    terminal_id = uuid4()
    snapshot = TmuxBindingReader(BindingRunner(), "$0").read(terminal_id)
    by_action = {binding.action: binding for binding in snapshot.bindings}
    assert snapshot.terminal_id == terminal_id
    assert snapshot.prefix == "C-a"
    assert snapshot.prefix2 is None
    assert by_action["split_left_right"].key == "C-a |"
    assert by_action["split_top_bottom"].key == "C-a -"
    assert by_action["new_window"].key == "C-a c"
    assert by_action["select_left"].key == "C-a h"
    assert by_action["toggle_zoom"].key == "C-a z"
    assert by_action["copy_mode"].key is None
    assert "unbound" in by_action["copy_mode"].tooltip.lower()
