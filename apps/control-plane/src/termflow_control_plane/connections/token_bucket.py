"""Deterministic byte-rate token bucket shared by WebSocket ingress paths."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class TokenBucket:
    rate: int
    tokens: float
    observed_at: float

    @classmethod
    def create(cls, rate: int) -> TokenBucket:
        return cls(rate=rate, tokens=float(rate), observed_at=time.monotonic())

    def consume(self, byte_count: int) -> bool:
        now = time.monotonic()
        self.tokens = min(
            float(self.rate),
            self.tokens + (now - self.observed_at) * self.rate,
        )
        self.observed_at = now
        if byte_count > self.tokens:
            return False
        self.tokens -= byte_count
        return True
