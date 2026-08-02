"""Async database lifecycle with fail-closed, packaged Alembic upgrades."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from sqlalchemy import Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import AuditEvent, Base, EnrollmentToken, Installation, Instance


class UnrecognizedDatabaseSchema(RuntimeError):
    """Raised instead of guessing how to mutate an unknown unversioned database."""


_CORE_TABLES: dict[str, Table] = {
    "enrollment_tokens": cast(Table, EnrollmentToken.__table__),
    "installations": cast(Table, Installation.__table__),
    "instances": cast(Table, Instance.__table__),
    "audit_events": cast(Table, AuditEvent.__table__),
}
_REQUIRED_UNVERSIONED_TABLES = {"enrollment_tokens", "installations", "instances"}
_V1_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "enrollment_tokens": {"id", "token_hash", "expires_at", "used_at", "created_at"},
    "installations": {"id", "token_hash", "created_at", "revoked_at"},
    "instances": {
        "id",
        "installation_id",
        "name",
        "token_hash",
        "created_at",
        "revoked_at",
    },
}
_V2_ADDITIONS: dict[str, dict[str, str]] = {
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


def _migration_config(connection: Connection) -> Config:
    migration_path = files("termflow_control_plane.persistence").joinpath("migrations")
    config = Config()
    config.set_main_option("script_location", str(migration_path))
    config.set_main_option("sqlalchemy.url", str(connection.engine.url))
    config.attributes["connection"] = connection
    return config


def _validate_known_unversioned_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    unexpected = table_names - set(_CORE_TABLES)
    if unexpected or not _REQUIRED_UNVERSIONED_TABLES <= table_names:
        raise UnrecognizedDatabaseSchema(
            "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
        )
    for table_name, required in _V1_REQUIRED_COLUMNS.items():
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        allowed = {column.name for column in _CORE_TABLES[table_name].columns}
        if not required <= columns or not columns <= allowed:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
            )


def _prepare_unversioned_v2(connection: Connection) -> None:
    _validate_known_unversioned_schema(connection)
    for table_name, columns in _V2_ADDITIONS.items():
        existing = {column["name"] for column in inspect(connection).get_columns(table_name)}
        for column_name, sql_type in columns.items():
            if column_name not in existing:
                connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")
                )
    cast(Table, AuditEvent.__table__).create(connection, checkfirst=True)
    for table_name, table in _CORE_TABLES.items():
        for index in table.indexes:
            index.create(connection, checkfirst=True)
        actual_columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
        required = {column.name for column in table.columns}
        if not required <= actual_columns:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
            )


def _validate_head_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    if actual_tables != expected_tables | {"alembic_version"}:
        raise UnrecognizedDatabaseSchema(
            "unrecognized versioned Control Plane database schema; refusing to start"
        )
    for table_name, table in Base.metadata.tables.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in table.columns}
        if actual_columns != expected_columns:
            raise UnrecognizedDatabaseSchema(
                "unrecognized versioned Control Plane database schema; refusing to start"
            )
    state_rows = connection.execute(
        text("SELECT id, epoch FROM authentication_state")
    ).all()
    if len(state_rows) != 1 or state_rows[0][0] != 1 or state_rows[0][1] < 1:
        raise UnrecognizedDatabaseSchema(
            "unrecognized authentication state; refusing to start"
        )


def _upgrade(connection: Connection) -> None:
    table_names = set(inspect(connection).get_table_names())
    config = _migration_config(connection)
    if table_names and "alembic_version" not in table_names:
        try:
            _prepare_unversioned_v2(connection)
        except IntegrityError as exc:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; "
                "refusing automatic upgrade"
            ) from exc
        command.stamp(config, "0001")
    command.upgrade(config, "head")
    _validate_head_schema(connection)


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
            await connection.run_sync(_upgrade)

    async def dispose(self) -> None:
        await self.engine.dispose()
