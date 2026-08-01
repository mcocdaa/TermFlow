"""Full-jitter reconnect delays scoped to one Instance."""

from __future__ import annotations

import random


class ReconnectBackoff:
    def __init__(
        self,
        *,
        base: float = 1.0,
        cap: float = 30.0,
        rng: random.Random | None = None,
    ) -> None:
        if base < 0 or cap < 0:
            raise ValueError("backoff bounds cannot be negative")
        self.base = base
        self.cap = cap
        self._rng = rng or random.Random()
        self.attempt = 0

    def next_delay(self) -> float:
        maximum = min(self.cap, self.base * (2**self.attempt))
        self.attempt += 1
        return self._rng.uniform(0, maximum)

    def reset(self) -> None:
        self.attempt = 0
