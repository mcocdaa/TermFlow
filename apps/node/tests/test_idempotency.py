from uuid import uuid4

import pytest
from termflow_node.bridge.idempotency import IdempotencyResults
from termflow_protocol import CommandResultPayload


@pytest.mark.asyncio
async def test_concurrent_duplicate_reservations_share_one_future() -> None:
    cache = IdempotencyResults(max_entries=2)
    key = uuid4()
    first = await cache.get_or_reserve(key)
    second = await cache.get_or_reserve(key)
    assert first.owner is True
    assert second.owner is False
    assert first.future is second.future
    result = CommandResultPayload(
        command_id=uuid4(),
        idempotency_key=key,
        ok=True,
    )
    await cache.complete(key, result)
    assert await second.future == result


@pytest.mark.asyncio
async def test_completed_results_are_bounded_lru() -> None:
    cache = IdempotencyResults(max_entries=2)
    keys = [uuid4() for _ in range(3)]
    for key in keys:
        reservation = await cache.get_or_reserve(key)
        await cache.complete(
            key,
            CommandResultPayload(command_id=uuid4(), idempotency_key=key, ok=True),
        )
        assert await reservation.future
    assert await cache.get(keys[0]) is None
    assert await cache.get(keys[2]) is not None
