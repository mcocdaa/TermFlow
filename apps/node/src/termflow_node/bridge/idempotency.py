"""Bounded in-memory command outcomes with concurrent reservation sharing."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from termflow_protocol import CommandResultPayload


@dataclass(frozen=True, slots=True)
class Reservation:
    owner: bool
    future: asyncio.Future[CommandResultPayload]


class IdempotencyResults:
    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[
            UUID,
            asyncio.Future[CommandResultPayload] | CommandResultPayload,
        ] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_reserve(self, key: UUID) -> Reservation:
        async with self._lock:
            existing = self._entries.get(key)
            if isinstance(existing, CommandResultPayload):
                self._entries.move_to_end(key)
                future = asyncio.get_running_loop().create_future()
                future.set_result(existing)
                return Reservation(False, future)
            if existing is not None:
                return Reservation(False, existing)
            future = asyncio.get_running_loop().create_future()
            self._entries[key] = future
            return Reservation(True, future)

    async def complete(self, key: UUID, result: CommandResultPayload) -> None:
        async with self._lock:
            existing = self._entries.get(key)
            if isinstance(existing, asyncio.Future) and not existing.done():
                existing.set_result(result)
            self._entries[key] = result
            self._entries.move_to_end(key)
            self._evict_completed()

    async def abort(self, key: UUID) -> None:
        async with self._lock:
            existing = self._entries.pop(key, None)
            if isinstance(existing, asyncio.Future) and not existing.done():
                existing.cancel()

    async def get(self, key: UUID) -> CommandResultPayload | None:
        async with self._lock:
            existing = self._entries.get(key)
            if isinstance(existing, CommandResultPayload):
                self._entries.move_to_end(key)
                return existing
            return None

    def _evict_completed(self) -> None:
        completed = sum(
            isinstance(value, CommandResultPayload) for value in self._entries.values()
        )
        if completed <= self._max_entries:
            return
        for key, value in tuple(self._entries.items()):
            if isinstance(value, CommandResultPayload):
                self._entries.pop(key)
                completed -= 1
                if completed <= self._max_entries:
                    return
