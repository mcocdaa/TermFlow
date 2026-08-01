"""Async database lifecycle."""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self) -> None:
        if self.engine.url.get_backend_name() == "sqlite":
            database_path = self.engine.url.database
            if database_path and database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as connection:
            if self.engine.url.get_backend_name() == "sqlite":
                await connection.execute(text("PRAGMA journal_mode=WAL"))
                await connection.execute(text("PRAGMA foreign_keys=ON"))
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
