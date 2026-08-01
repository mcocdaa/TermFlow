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

_SQLITE_V2_COLUMNS: dict[str, dict[str, str]] = {
    "enrollment_tokens": {"display_name": "VARCHAR(128)"},
    "installations": {
        "hostname": "VARCHAR(255)",
        "display_name": "VARCHAR(128)",
        "platform": "VARCHAR(128)",
        "client_version": "VARCHAR(64)",
        "last_seen_at": "DATETIME",
    },
    "instances": {"last_seen_at": "DATETIME"},
}


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
            if self.engine.url.get_backend_name() == "sqlite":
                await self._migrate_sqlite_v2(connection)

    @staticmethod
    async def _migrate_sqlite_v2(connection) -> None:  # type: ignore[no-untyped-def]
        for table, columns in _SQLITE_V2_COLUMNS.items():
            result = await connection.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result}
            for column, sql_type in columns.items():
                if column not in existing:
                    await connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    )

    async def dispose(self) -> None:
        await self.engine.dispose()
