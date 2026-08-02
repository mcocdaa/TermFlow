"""Cancellation-safe reads for authentication message boundaries."""

from __future__ import annotations

import asyncio

from anyio import CancelScope

from termflow_control_plane.persistence.repositories import RepositoryBundle


async def persisted_authentication_epoch(repositories: RepositoryBundle) -> int:
    """Finish the short DB read before propagating transport cancellation."""

    lookup = asyncio.create_task(repositories.auth_state.get())
    try:
        state = await asyncio.shield(lookup)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            await lookup
        raise
    return state.epoch
