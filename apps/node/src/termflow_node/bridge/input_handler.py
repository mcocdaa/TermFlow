"""Per-Pane serialized execution of validated literal text."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from termflow_protocol import CommandResultPayload, PaneInputPayload, TopologySnapshot

from termflow_node.tmux.runner import TmuxCommandError, TmuxRunner

from .idempotency import IdempotencyResults


class TextSender(Protocol):
    async def send_text(self, pane_id: str, text: str, submit: bool) -> None: ...


class AsyncTmuxInput:
    def __init__(self, runner: TmuxRunner) -> None:
        self._runner = runner

    async def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        await asyncio.to_thread(self._runner.send_text, pane_id, text, submit)


class InputHandler:
    def __init__(
        self,
        *,
        topology_provider: Callable[[], TopologySnapshot],
        sender: TextSender,
        idempotency: IdempotencyResults | None = None,
    ) -> None:
        self._topology_provider = topology_provider
        self._sender = sender
        self._idempotency = idempotency or IdempotencyResults()
        self._pane_locks: dict[str, asyncio.Lock] = {}

    async def handle(self, command: PaneInputPayload) -> CommandResultPayload:
        reservation = await self._idempotency.get_or_reserve(command.idempotency_key)
        if not reservation.owner:
            cached = await reservation.future
            return cached.model_copy(update={"command_id": command.command_id})
        try:
            result = await self._execute(command)
            await self._idempotency.complete(command.idempotency_key, result)
            return result
        except asyncio.CancelledError:
            await self._idempotency.abort(command.idempotency_key)
            raise

    async def _execute(self, command: PaneInputPayload) -> CommandResultPayload:
        lock = self._pane_locks.setdefault(command.pane_id, asyncio.Lock())
        async with lock:
            if not self._topology_provider().contains_pane(command.pane_id):
                return self._result(command, ok=False, error_code="pane_not_found")
            try:
                await self._sender.send_text(command.pane_id, command.text, command.submit)
            except TmuxCommandError:
                error_code = (
                    "connection_lost"
                    if self._topology_provider().contains_pane(command.pane_id)
                    else "pane_not_found"
                )
                return self._result(command, ok=False, error_code=error_code)
            return self._result(command, ok=True, error_code=None)

    @staticmethod
    def _result(
        command: PaneInputPayload,
        *,
        ok: bool,
        error_code: str | None,
    ) -> CommandResultPayload:
        return CommandResultPayload(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            ok=ok,
            error_code=error_code,
        )

    def retain_panes(self, pane_ids: set[str]) -> None:
        for pane_id in set(self._pane_locks) - pane_ids:
            if not self._pane_locks[pane_id].locked():
                self._pane_locks.pop(pane_id, None)
