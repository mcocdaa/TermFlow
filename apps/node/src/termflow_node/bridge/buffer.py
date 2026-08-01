"""Exact byte-bounded, memory-only Pane output replay."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class OutputChunk:
    stream_id: UUID
    seq: int
    data: bytes
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class ReplayGap:
    reason: Literal["stream_changed", "overwritten"]


class PaneOutputBuffer:
    def __init__(self, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.stream_id = uuid4()
        self._next_seq = 1
        self._chunks: deque[OutputChunk] = deque()
        self._total_bytes = 0
        self._overwritten_through = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def append(self, data: bytes) -> OutputChunk:
        seq = self._next_seq
        self._next_seq += 1
        stored = data
        if len(stored) > self.max_bytes:
            self._chunks.clear()
            self._total_bytes = 0
            self._overwritten_through = seq
            stored = stored[-self.max_bytes :]
        chunk = OutputChunk(
            stream_id=self.stream_id,
            seq=seq,
            data=stored,
            captured_at=datetime.now(UTC),
        )
        self._chunks.append(chunk)
        self._total_bytes += len(stored)
        while self._total_bytes > self.max_bytes:
            removed = self._chunks.popleft()
            self._total_bytes -= len(removed.data)
            self._overwritten_through = max(self._overwritten_through, removed.seq)
        return chunk

    def replay(self, stream_id: UUID, after_seq: int) -> list[OutputChunk] | ReplayGap:
        if stream_id != self.stream_id:
            return ReplayGap(reason="stream_changed")
        if after_seq < self._overwritten_through:
            return ReplayGap(reason="overwritten")
        return [chunk for chunk in self._chunks if chunk.seq > after_seq]


class OutputBuffers:
    def __init__(self, *, max_bytes_per_pane: int) -> None:
        if max_bytes_per_pane < 1:
            raise ValueError("max_bytes_per_pane must be positive")
        self._max_bytes = max_bytes_per_pane
        self._buffers: dict[str, PaneOutputBuffer] = {}

    @property
    def total_bytes(self) -> int:
        return sum(buffer.total_bytes for buffer in self._buffers.values())

    @property
    def pane_ids(self) -> set[str]:
        return set(self._buffers)

    def _get_or_create(self, pane_id: str) -> PaneOutputBuffer:
        buffer = self._buffers.get(pane_id)
        if buffer is None:
            buffer = PaneOutputBuffer(max_bytes=self._max_bytes)
            self._buffers[pane_id] = buffer
        return buffer

    def for_pane(self, pane_id: str) -> PaneOutputBuffer:
        return self._get_or_create(pane_id)

    def append(self, pane_id: str, data: bytes) -> OutputChunk:
        return self._get_or_create(pane_id).append(data)

    def replay(
        self,
        pane_id: str,
        stream_id: UUID,
        after_seq: int,
    ) -> list[OutputChunk] | ReplayGap:
        buffer = self._buffers.get(pane_id)
        if buffer is None:
            return ReplayGap(reason="stream_changed")
        return buffer.replay(stream_id, after_seq)

    def remove(self, pane_id: str) -> None:
        self._buffers.pop(pane_id, None)

    def reset_stream(self, pane_id: str) -> UUID:
        buffer = PaneOutputBuffer(max_bytes=self._max_bytes)
        self._buffers[pane_id] = buffer
        return buffer.stream_id
