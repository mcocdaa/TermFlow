"""Discover tmux Prefix options and semantic-action key bindings."""

from __future__ import annotations

import shlex
from typing import Protocol
from uuid import UUID

from termflow_protocol import (
    TerminalAction,
    TerminalBinding,
    TerminalBindingsPayload,
)


class BindingRunner(Protocol):
    def run_command(self, *arguments: str): ...  # type: ignore[no-untyped-def]


_ACTIONS: tuple[TerminalAction, ...] = (
    "split_left_right",
    "split_top_bottom",
    "new_window",
    "select_left",
    "select_right",
    "select_up",
    "select_down",
    "toggle_zoom",
    "copy_mode",
    "close_pane",
)
_LABELS: dict[TerminalAction, str] = {
    "split_left_right": "Split left/right",
    "split_top_bottom": "Split top/bottom",
    "new_window": "New Window",
    "select_left": "Select left Pane",
    "select_right": "Select right Pane",
    "select_up": "Select upper Pane",
    "select_down": "Select lower Pane",
    "toggle_zoom": "Toggle Pane zoom",
    "copy_mode": "Enter copy mode",
    "close_pane": "Close Pane",
}


def _semantic_action(command: list[str]) -> TerminalAction | None:
    if not command:
        return None
    executable = command[0]
    arguments = set(command[1:])
    if executable == "split-window":
        if "-h" in arguments:
            return "split_left_right"
        # tmux's default vertical-split binding invokes bare `split-window`.
        return "split_top_bottom"
    if executable == "new-window":
        return "new_window"
    if executable == "select-pane":
        for option, action in (
            ("-L", "select_left"),
            ("-R", "select_right"),
            ("-U", "select_up"),
            ("-D", "select_down"),
        ):
            if option in arguments:
                return action  # type: ignore[return-value]
    if executable == "resize-pane" and "-Z" in arguments:
        return "toggle_zoom"
    if executable == "copy-mode":
        return "copy_mode"
    if executable == "kill-pane":
        return "close_pane"
    return None


def _parse_prefix_bindings(output: str) -> dict[TerminalAction, str]:
    bindings: dict[TerminalAction, str] = {}
    for line in output.splitlines():
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        if not words or words[0] != "bind-key" or "-T" not in words:
            continue
        table_index = words.index("-T")
        if len(words) <= table_index + 3 or words[table_index + 1] != "prefix":
            continue
        key = words[table_index + 2]
        action = _semantic_action(words[table_index + 3 :])
        if action is not None and action not in bindings:
            bindings[action] = key
    return bindings


class TmuxBindingReader:
    def __init__(self, runner: BindingRunner, session_id: str) -> None:
        self._runner = runner
        self._session_id = session_id

    def _option(self, name: str) -> str | None:
        value = self._runner.run_command(
            "show-options",
            "-gv",
            "-t",
            self._session_id,
            name,
        ).stdout.strip()
        return None if not value or value == "None" else value

    def read(self, terminal_id: UUID) -> TerminalBindingsPayload:
        prefix = self._option("prefix") or "C-b"
        prefix2 = self._option("prefix2")
        output = self._runner.run_command("list-keys", "-T", "prefix").stdout
        detected = _parse_prefix_bindings(output)
        bindings = [
            TerminalBinding(
                action=action,
                key=f"{prefix} {detected[action]}" if action in detected else None,
                tooltip=(
                    f"{_LABELS[action]} ({prefix} {detected[action]})"
                    if action in detected
                    else f"{_LABELS[action]} (unbound)"
                ),
            )
            for action in _ACTIONS
        ]
        return TerminalBindingsPayload(
            terminal_id=terminal_id,
            prefix=prefix,
            prefix2=prefix2,
            bindings=bindings,
        )
