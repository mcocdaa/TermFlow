"""Per-Pane serialized execution of validated literal text and named keys."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from termflow_protocol import (
    CommandResultPayload,
    PaneInputPayload,
    PaneKeyInputPayload,
    TopologySnapshot,
)

from termflow_node.tmux.runner import TmuxCommandError, TmuxRunner

from .idempotency import IdempotencyResults
from .key_names import tmux_key_names


class TextSender(Protocol):
    async def send_text(self, pane_id: str, text: str, submit: bool) -> None: ...


class KeysSender(Protocol):
    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> None: ...


class AsyncTmuxInput:
    def __init__(self, runner: TmuxRunner) -> None:
        self._runner = runner

    async def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        await asyncio.to_thread(self._runner.send_text, pane_id, text, submit)

    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> None:
        await asyncio.to_thread(self._runner.send_keys, pane_id, keys)


class InputHandler:
    def __init__(
        self,
        *,
        topology_provider: Callable[[], TopologySnapshot],
        sender: TextSender,
        keys_sender: KeysSender | None = None,
        idempotency: IdempotencyResults | None = None,
    ) -> None:
        self._topology_provider = topology_provider
        self._sender = sender
        self._keys_sender = keys_sender or sender
        self._idempotency = idempotency or IdempotencyResults()
        self._pane_locks: dict[str, asyncio.Lock] = {}

    async def handle(self, command: PaneInputPayload) -> CommandResultPayload:
        return await self._handle(command, lambda: self._send_text(command))

    async def handle_keys(self, command: PaneKeyInputPayload) -> CommandResultPayload:
        return await self._handle(command, lambda: self._send_keys(command))

    async def _handle(
        self,
        command: PaneInputPayload | PaneKeyInputPayload,
        execution: Callable[[], Awaitable[CommandResultPayload]],
    ) -> CommandResultPayload:
        reservation = await self._idempotency.get_or_reserve(command.idempotency_key)
        if not reservation.owner:
            cached = await reservation.future
            return cached.model_copy(update={"command_id": command.command_id})
        try:
            result = await execution()
            await self._idempotency.complete(command.idempotency_key, result)
            return result
        except asyncio.CancelledError:
            await self._idempotency.abort(command.idempotency_key)
            raise

    async def _send_text(self, command: PaneInputPayload) -> CommandResultPayload:
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

    async def _send_keys(self, command: PaneKeyInputPayload) -> CommandResultPayload:
        # Text and key input share the per-pane lock: a pane's writes are
        # serialized regardless of their type (M5.2).
        lock = self._pane_locks.setdefault(command.pane_id, asyncio.Lock())
        async with lock:
            if not self._topology_provider().contains_pane(command.pane_id):
                return self._result(command, ok=False, error_code="pane_not_found")
            try:
                tmux_names = tmux_key_names(command.keys)
            except KeyError:
                # Unknown names fail closed: never pass an unmapped string
                # to tmux (defense in depth; the wire model rejects them
                # first).
                return self._result(command, ok=False, error_code="invalid_key")
            try:
                await self._keys_sender.send_keys(command.pane_id, tmux_names)
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
        command: PaneInputPayload | PaneKeyInputPayload,
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
