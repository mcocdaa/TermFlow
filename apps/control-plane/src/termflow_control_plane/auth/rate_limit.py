"""Bounded authentication request limits with deterministic backoff."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from termflow_control_plane.errors import TermFlowError


@dataclass(slots=True)
class _SourceState:
    tokens: float
    updated_at: float
    last_seen_at: float
    failures: int = 0
    next_allowed_at: float = 0.0


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


def direct_peer_source(request: Request) -> str:
    """Return only the ASGI peer address, deliberately ignoring proxy headers."""

    return request.client.host if request.client is not None else "unknown-peer"


class AuthRateLimiter:
    """Per-source and global token buckets plus progressive failure delays."""

    def __init__(
        self,
        *,
        capacity: int = 5,
        refill_seconds: float = 60.0,
        global_capacity: int = 100,
        global_refill_seconds: float = 1.0,
        max_backoff_seconds: int = 300,
        max_entries: int = 4096,
        state_ttl_seconds: float = 900.0,
        max_concurrent_verifications: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or global_capacity < 1:
            raise ValueError("authentication rate-limit capacities must be positive")
        if refill_seconds <= 0 or global_refill_seconds <= 0:
            raise ValueError("authentication refill periods must be positive")
        if max_backoff_seconds < 1:
            raise ValueError("maximum authentication backoff must be positive")
        if max_entries < 1:
            raise ValueError("authentication rate-limit state capacity must be positive")
        if state_ttl_seconds <= max_backoff_seconds:
            raise ValueError("authentication state TTL must exceed maximum backoff")
        if max_concurrent_verifications < 1:
            raise ValueError("authentication verification concurrency must be positive")

        self._capacity = capacity
        self._refill_seconds = refill_seconds
        self._global_capacity = global_capacity
        self._global_refill_seconds = global_refill_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._max_entries = max_entries
        self._state_ttl_seconds = state_ttl_seconds
        self._clock = clock
        now = clock()
        self._global_bucket = _Bucket(tokens=float(global_capacity), updated_at=now)
        self._states: OrderedDict[tuple[str, str], _SourceState] = OrderedDict()
        self._state_lock = Lock()
        self._verification_slots = asyncio.BoundedSemaphore(max_concurrent_verifications)

    @property
    def tracked_source_count(self) -> int:
        with self._state_lock:
            return len(self._states)

    def check(self, purpose: str, source: str) -> None:
        """Consume one permitted authentication request or raise a generic 429."""

        key = self._key(purpose, source)
        now = self._clock()
        with self._state_lock:
            self._prune_locked(now)
            self._refill_global(now)
            global_wait = self._bucket_wait(
                self._global_bucket.tokens,
                self._global_refill_seconds,
            )
            if global_wait > 0:
                raise self._limited(global_wait)

            state = self._state_locked(key, now)
            self._refill_source(state, now)
            wait = max(
                state.next_allowed_at - now,
                self._bucket_wait(state.tokens, self._refill_seconds),
            )
            if wait > 0:
                self._touch_locked(key, state, now)
                raise self._limited(wait)

            self._global_bucket.tokens -= 1.0
            state.tokens -= 1.0
            self._touch_locked(key, state, now)

    def record_failure(self, purpose: str, source: str) -> int:
        """Record one failed verification and return its progressive delay."""

        key = self._key(purpose, source)
        now = self._clock()
        with self._state_lock:
            self._prune_locked(now)
            state = self._state_locked(key, now)
            state.failures = min(
                state.failures + 1,
                self._max_backoff_seconds.bit_length() + 1,
            )
            delay: int = min(1 << (state.failures - 1), self._max_backoff_seconds)
            state.next_allowed_at = now + delay
            self._touch_locked(key, state, now)
            return delay

    def record_success(self, purpose: str, source: str) -> None:
        """Clear failure delay for exactly one source and authentication purpose."""

        key = self._key(purpose, source)
        now = self._clock()
        with self._state_lock:
            self._prune_locked(now)
            state = self._states.get(key)
            if state is None:
                return
            state.failures = 0
            state.next_allowed_at = 0.0
            self._touch_locked(key, state, now)

    def prune(self) -> None:
        """Remove stale source state without exposing source identifiers."""

        with self._state_lock:
            self._prune_locked(self._clock())

    @asynccontextmanager
    async def verification_slot(self) -> AsyncIterator[None]:
        """Reject excess expensive verifications immediately instead of queueing them."""

        if self._verification_slots.locked():
            raise self._limited(1.0)
        await self._verification_slots.acquire()
        try:
            yield
        finally:
            self._verification_slots.release()

    @staticmethod
    def _key(purpose: str, source: str) -> tuple[str, str]:
        if not purpose or not source:
            raise ValueError("authentication purpose and source must be non-empty")
        return purpose, source

    def _state_locked(self, key: tuple[str, str], now: float) -> _SourceState:
        state = self._states.get(key)
        if state is not None:
            return state
        while len(self._states) >= self._max_entries:
            self._states.popitem(last=False)
        state = _SourceState(
            tokens=float(self._capacity),
            updated_at=now,
            last_seen_at=now,
        )
        self._states[key] = state
        return state

    def _touch_locked(
        self,
        key: tuple[str, str],
        state: _SourceState,
        now: float,
    ) -> None:
        state.last_seen_at = now
        self._states.move_to_end(key)

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, state in self._states.items()
            if now - state.last_seen_at >= self._state_ttl_seconds
        ]
        for key in expired:
            del self._states[key]

    def _refill_source(self, state: _SourceState, now: float) -> None:
        elapsed = max(0.0, now - state.updated_at)
        state.tokens = min(
            float(self._capacity),
            state.tokens + elapsed / self._refill_seconds,
        )
        state.updated_at = now

    def _refill_global(self, now: float) -> None:
        elapsed = max(0.0, now - self._global_bucket.updated_at)
        self._global_bucket.tokens = min(
            float(self._global_capacity),
            self._global_bucket.tokens + elapsed / self._global_refill_seconds,
        )
        self._global_bucket.updated_at = now

    @staticmethod
    def _bucket_wait(tokens: float, refill_seconds: float) -> float:
        if tokens >= 1.0:
            return 0.0
        return (1.0 - tokens) * refill_seconds

    @staticmethod
    def _limited(wait_seconds: float) -> TermFlowError:
        return TermFlowError(
            code="rate_limited",
            status_code=429,
            message="Authentication is temporarily unavailable.",
            retry_after=max(1, math.ceil(wait_seconds)),
        )
