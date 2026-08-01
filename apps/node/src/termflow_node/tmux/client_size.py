"""Resolve the A-authoritative character grid from local tmux clients."""

from dataclasses import dataclass
from typing import Protocol

from termflow_node.tmux.runner import TmuxClient


@dataclass(frozen=True, slots=True)
class TerminalSize:
    rows: int
    cols: int

    def __post_init__(self) -> None:
        if self.rows < 1 or self.cols < 1:
            raise ValueError("terminal size must be positive")


class ClientRunner(Protocol):
    def list_clients(self, session_id: str) -> list[TmuxClient]: ...


class ClientSizeResolver:
    def __init__(
        self,
        runner: ClientRunner,
        session_id: str,
        *,
        creation_size: TerminalSize | None = None,
    ) -> None:
        self._runner = runner
        self._session_id = session_id
        self._creation_size = creation_size
        self._last_observed: TerminalSize | None = None

    def resolve(self, *, proxy_ttys: set[str]) -> TerminalSize:
        candidates = [
            client
            for client in self._runner.list_clients(self._session_id)
            if (
                client.tty not in proxy_ttys
                and not client.control_mode
                and client.rows > 0
                and client.cols > 0
            )
        ]
        if candidates:
            latest = max(candidates, key=lambda client: client.activity)
            self._last_observed = TerminalSize(latest.rows, latest.cols)
        return (
            self._last_observed
            or self._creation_size
            or TerminalSize(rows=24, cols=80)
        )
