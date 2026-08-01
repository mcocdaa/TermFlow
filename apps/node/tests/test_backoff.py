import random

from termflow_node.bridge.backoff import ReconnectBackoff


def test_backoff_is_capped_and_resettable() -> None:
    backoff = ReconnectBackoff(base=1, cap=30, rng=random.Random(7))
    delays = [backoff.next_delay() for _ in range(20)]
    assert all(0 <= delay <= 30 for delay in delays)
    assert backoff.attempt == 20
    backoff.reset()
    assert backoff.attempt == 0
