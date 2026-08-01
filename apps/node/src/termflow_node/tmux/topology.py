"""Deterministic snapshots of the one managed tmux Session."""

from __future__ import annotations

import shlex
from collections import defaultdict
from typing import Protocol

from termflow_protocol import PaneSnapshot, TopologySnapshot, WindowSnapshot


class TopologyQueryError(RuntimeError):
    pass


class QueryRunner(Protocol):
    def run_command(self, *arguments: str): ...  # type: ignore[no-untyped-def]


_WINDOW_FORMAT = " ".join(
    (
        "#{session_id}",
        "#{q:session_name}",
        "#{window_id}",
        "#{window_index}",
        "#{window_active}",
        "#{q:window_name}",
    )
)
_PANE_FORMAT = " ".join(
    (
        "#{session_id}",
        "#{window_id}",
        "#{pane_id}",
        "#{pane_index}",
        "#{pane_active}",
        "#{pane_dead}",
        "#{pane_width}",
        "#{pane_height}",
        "#{q:pane_title}",
    )
)


class TopologyReader:
    def __init__(self, runner: QueryRunner) -> None:
        self._runner = runner
        self._current: TopologySnapshot | None = None

    @staticmethod
    def _fields(line: str, expected: int) -> list[str]:
        try:
            fields = shlex.split(line)
        except ValueError as exc:
            raise TopologyQueryError("tmux returned malformed quoted topology") from exc
        if len(fields) != expected:
            raise TopologyQueryError(
                f"tmux topology row has {len(fields)} fields; expected {expected}"
            )
        return fields

    def read(self) -> TopologySnapshot:
        window_result = self._runner.run_command("list-windows", "-F", _WINDOW_FORMAT)
        pane_result = self._runner.run_command("list-panes", "-a", "-F", _PANE_FORMAT)
        window_rows = [self._fields(line, 6) for line in window_result.stdout.splitlines() if line]
        pane_rows = [self._fields(line, 9) for line in pane_result.stdout.splitlines() if line]
        if not window_rows:
            raise TopologyQueryError("managed Session has no windows")
        session_ids = {row[0] for row in window_rows} | {row[0] for row in pane_rows}
        if len(session_ids) != 1:
            raise TopologyQueryError("topology must contain exactly one managed Session")
        session_id = next(iter(session_ids))
        session_names = {row[1] for row in window_rows}
        if len(session_names) != 1:
            raise TopologyQueryError("managed Session name is inconsistent")

        panes_by_window: dict[str, list[PaneSnapshot]] = defaultdict(list)
        for row in pane_rows:
            panes_by_window[row[1]].append(
                PaneSnapshot(
                    pane_id=row[2],
                    window_id=row[1],
                    index=int(row[3]),
                    active=row[4] == "1",
                    dead=row[5] == "1",
                    width=int(row[6]),
                    height=int(row[7]),
                    title=row[8],
                )
            )
        windows = [
            WindowSnapshot(
                window_id=row[2],
                index=int(row[3]),
                active=row[4] == "1",
                name=row[5],
                panes=sorted(panes_by_window[row[2]], key=lambda pane: pane.index),
            )
            for row in window_rows
        ]
        candidate = TopologySnapshot(
            session_id=session_id,
            session_name=next(iter(session_names)),
            revision=0,
            windows=sorted(windows, key=lambda window: window.index),
        )
        if self._current is not None:
            previous_value = self._current.model_copy(update={"revision": 0})
            if candidate == previous_value:
                return self._current
            revision = self._current.revision + 1
        else:
            revision = 1
        self._current = candidate.model_copy(update={"revision": revision})
        return self._current
