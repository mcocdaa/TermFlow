"""Small repository race/error-path tests that do not require SQLite."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from termflow_control_plane.persistence.repositories import WatchDeliveryRepository


class _FailingSession:
    def add(self, value: object) -> None:
        del value

    async def commit(self) -> None:
        raise IntegrityError("insert", {}, RuntimeError("duplicate"))


class _SessionContext:
    async def __aenter__(self) -> _FailingSession:
        return _FailingSession()

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_create_unique_fails_closed_if_integrity_race_has_no_existing_row() -> None:
    repository = WatchDeliveryRepository(lambda: _SessionContext())  # type: ignore[arg-type]
    repository.get_by_key = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="uniqueness conflict"):
        import asyncio

        asyncio.run(
            repository.create_unique(
                watch_id=uuid4(),
                delivery_key="delivery-race",
            )
        )
