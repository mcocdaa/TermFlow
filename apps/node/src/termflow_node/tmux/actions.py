"""Validated semantic actions mapped to direct tmux argv."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from termflow_protocol import (
    TerminalActionPayload,
    TermRenameRequest,
    TopologySnapshot,
)

from termflow_node.instances.store import InstanceStore


class ActionRunner(Protocol):
    def run_command(self, *arguments: str): ...  # type: ignore[no-untyped-def]


class RenameRunner(ActionRunner, Protocol):
    def rename_session(self, target: str, name: str) -> None: ...

    def session_identity(self, target: str | None = None): ...  # type: ignore[no-untyped-def]


class ActionRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_PANE_ACTIONS: dict[str, tuple[str, ...]] = {
    "split_left_right": ("split-window", "-h", "-t"),
    "split_top_bottom": ("split-window", "-v", "-t"),
    "toggle_zoom": ("resize-pane", "-Z", "-t"),
    "copy_mode": ("copy-mode", "-t"),
    "close_pane": ("kill-pane", "-t"),
}
_DIRECTION_ACTIONS = {
    "select_left": "-L",
    "select_right": "-R",
    "select_up": "-U",
    "select_down": "-D",
}


class TmuxActionExecutor:
    def __init__(
        self,
        runner: ActionRunner,
        session_id: str,
        *,
        topology_provider: Callable[[], TopologySnapshot],
    ) -> None:
        self._runner = runner
        self._session_id = session_id
        self._topology_provider = topology_provider

    def _validated_pane(self, pane_id: str | None) -> str:
        if pane_id is None:
            raise ActionRejected("pane_not_found")
        topology = self._topology_provider()
        pane = next(
            (
                pane
                for window in topology.windows
                for pane in window.panes
                if pane.pane_id == pane_id
            ),
            None,
        )
        if pane is None or pane.dead:
            raise ActionRejected("pane_not_found")
        return pane_id

    def execute(self, action: TerminalActionPayload) -> None:
        if action.action == "new_window":
            self._runner.run_command("new-window", "-t", self._session_id)
            return
        pane_id = self._validated_pane(action.target_pane_id)
        if action.action in _DIRECTION_ACTIONS:
            self._runner.run_command(
                "select-pane",
                "-t",
                pane_id,
                _DIRECTION_ACTIONS[action.action],
            )
            return
        arguments = _PANE_ACTIONS.get(action.action)
        if arguments is None:
            raise ActionRejected("unsupported_action")
        self._runner.run_command(*arguments, pane_id)


class TermRenamer:
    def __init__(
        self,
        *,
        runner: RenameRunner,
        store: InstanceStore,
        instance_id: UUID,
        topology_provider: Callable[[], TopologySnapshot],
    ) -> None:
        self._runner = runner
        self._store = store
        self._instance_id = instance_id
        self._topology_provider = topology_provider

    def rename(self, name: str) -> TopologySnapshot:
        validated_name = TermRenameRequest(name=name).name
        record = self._store.load(self._instance_id)
        if record.session_id is None:
            raise ActionRejected("session_identity_unavailable")
        self._runner.rename_session(record.session_id, validated_name)
        identity = self._runner.session_identity(record.session_id)
        updated = record.model_copy(
            update={
                "schema_version": 4,
                "name": identity.session_name,
                "session_name": identity.session_name,
                "session_id": identity.session_id,
            }
        )
        self._store.save(updated)
        return self._topology_provider()
