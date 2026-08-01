"""Cancellation-safe, metadata-only terminal audit buffering."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class AuditRepository(Protocol):
    async def record(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _AuditRecord:
    operation: str
    instance_id: UUID | None
    pane_id: str | None
    input_bytes: int | None
    result: str
    error_code: str | None


class TerminalAuditWriter:
    """Serialize terminal metadata writes outside cancellable WebSocket tasks."""

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository
        self._queue: asyncio.Queue[_AuditRecord | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._closed = False

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("terminal audit writer already started")
        self._worker = asyncio.create_task(self._run())

    def record_nowait(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> None:
        if self._closed:
            raise RuntimeError("terminal audit writer is closed")
        self._queue.put_nowait(
            _AuditRecord(
                operation=operation,
                instance_id=instance_id,
                pane_id=pane_id,
                input_bytes=input_bytes,
                result=result,
                error_code=error_code,
            )
        )

    async def _run(self) -> None:
        while True:
            record = await self._queue.get()
            try:
                if record is None:
                    return
                await self._repository.record(
                    record.operation,
                    record.instance_id,
                    record.pane_id,
                    record.input_bytes,
                    record.result,
                    record.error_code,
                )
            except Exception as exc:
                if self._failure is None:
                    self._failure = exc
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()
        if self._failure is not None:
            raise RuntimeError("terminal audit persistence failed") from self._failure

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
        await self._queue.join()
        assert self._worker is not None
        await self._worker
        if self._failure is not None:
            raise RuntimeError("terminal audit persistence failed") from self._failure
