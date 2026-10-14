"""Cancellation-safe reads for authentication message boundaries."""

from __future__ import annotations

import asyncio

from anyio import CancelScope

from termflow_control_plane.persistence.repositories import RepositoryBundle

_EPOCH_CACHE_TTL_SECONDS = 1.0


async def persisted_authentication_epoch(
    repositories: RepositoryBundle,
    *,
    ttl_seconds: float = _EPOCH_CACHE_TTL_SECONDS,
) -> int:
    """Finish the short DB read before propagating transport cancellation."""

    auth_state = repositories.auth_state
    cached = getattr(auth_state, "cached_epoch", None)
    if callable(cached):
        epoch = cached(ttl_seconds=ttl_seconds)
        if isinstance(epoch, int):
            return epoch

    lookup = asyncio.create_task(auth_state.get())
    try:
        state = await asyncio.shield(lookup)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            await lookup
        raise
    return state.epoch
