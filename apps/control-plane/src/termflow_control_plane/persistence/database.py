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
_V1_TYPES: dict[str, dict[str, str]] = {
    "enrollment_tokens": {
        "id": "CHAR(32)",
        "token_hash": "VARCHAR(64)",
        "expires_at": "DATETIME",
        "used_at": "DATETIME",
        "created_at": "DATETIME",
    },
    "installations": {
        "id": "CHAR(32)",
        "token_hash": "VARCHAR(64)",
        "created_at": "DATETIME",
        "revoked_at": "DATETIME",
    },
    "instances": {
        "id": "CHAR(32)",
        "installation_id": "CHAR(32)",
        "name": "VARCHAR(128)",
        "token_hash": "VARCHAR(64)",
        "created_at": "DATETIME",
        "revoked_at": "DATETIME",
    },
}
_V1_NOT_NULL: dict[str, set[str]] = {
    "enrollment_tokens": {"token_hash", "expires_at", "created_at"},
    "installations": {"token_hash", "created_at"},
    "instances": {"installation_id", "name", "token_hash", "created_at"},
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
        if connection.dialect.name == "sqlite":
            inspected = {column["name"]: column for column in inspector.get_columns(table_name)}
            for column_name, expected_type in _V1_TYPES[table_name].items():
                column = inspected[column_name]
                if str(column["type"]).upper() != expected_type:
                    raise UnrecognizedDatabaseSchema(
                        "unrecognized unversioned Control Plane database schema; "
                        "refusing automatic upgrade"
                    )
                if column_name in _V1_NOT_NULL[table_name] and column["nullable"]:
                    raise UnrecognizedDatabaseSchema(
                        "unrecognized unversioned Control Plane database schema; "
                        "refusing automatic upgrade"
                    )
            for column_name, expected_type in _V2_ADDITIONS[table_name].items():
                if (
                    column_name in inspected
                    and str(inspected[column_name]["type"]).upper() != expected_type
                ):
                    raise UnrecognizedDatabaseSchema(
                        "unrecognized unversioned Control Plane database schema; "
                        "refusing automatic upgrade"
                    )

    if "audit_events" in table_names:
        audit_columns = {column["name"] for column in inspector.get_columns("audit_events")}
        if audit_columns != {column.name for column in _CORE_TABLES["audit_events"].columns}:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; "
                "refusing automatic upgrade"
            )

    v2_instance_columns = {
        column["name"] for column in inspector.get_columns("instances")
    }
    if set(_V2_ADDITIONS["instances"]) <= v2_instance_columns:
        ownership_foreign_keys = inspector.get_foreign_keys("instances")
        if ownership_foreign_keys and not any(
            foreign_key.get("referred_table") == "installations"
            and foreign_key.get("constrained_columns") == ["installation_id"]
            for foreign_key in ownership_foreign_keys
        ):
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; "
                "refusing automatic upgrade"
            )
    for table_name in _REQUIRED_UNVERSIONED_TABLES:
        duplicate = connection.execute(
            text(
                f"SELECT 1 FROM {table_name} GROUP BY token_hash "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; "
                "refusing automatic upgrade"
            )
    orphan = connection.execute(
        text(
            "SELECT 1 FROM instances LEFT JOIN installations "
            "ON installations.id = instances.installation_id "
            "WHERE installations.id IS NULL LIMIT 1"
        )
    ).first()
    if orphan is not None:
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
    if connection.dialect.name == "sqlite" and not inspect(connection).get_foreign_keys(
        "instances"
    ):
        connection.execute(
            text(
                """
                CREATE TABLE instances__termflow_v2 (
                  id CHAR(32) NOT NULL PRIMARY KEY,
                  installation_id CHAR(32) NOT NULL,
                  name VARCHAR(128) NOT NULL,
                  token_hash VARCHAR(64) NOT NULL,
                  last_seen_at DATETIME,
                  created_at DATETIME NOT NULL,
                  revoked_at DATETIME,
                  FOREIGN KEY(installation_id) REFERENCES installations(id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO instances__termflow_v2 (
                  id, installation_id, name, token_hash, last_seen_at, created_at, revoked_at
                )
                SELECT id, installation_id, name, token_hash, last_seen_at, created_at, revoked_at
                FROM instances
                """
            )
        )
        connection.execute(text("DROP TABLE instances"))
        connection.execute(
            text("ALTER TABLE instances__termflow_v2 RENAME TO instances")
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
        inspected_columns = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        actual_columns = set(inspected_columns)
        expected_columns = {column.name for column in table.columns}
        if actual_columns != expected_columns:
            raise UnrecognizedDatabaseSchema(
                "unrecognized versioned Control Plane database schema; refusing to start"
            )
        for column in table.columns:
            inspected_column = inspected_columns[column.name]
            if str(inspected_column["type"]).upper() != str(column.type).upper():
                raise UnrecognizedDatabaseSchema(
                    "unrecognized versioned Control Plane database schema; refusing to start"
                )
            if not column.primary_key and inspected_column["nullable"] != column.nullable:
                raise UnrecognizedDatabaseSchema(
                    "unrecognized versioned Control Plane database schema; refusing to start"
                )
        primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns")
        if primary_key != [column.name for column in table.primary_key.columns]:
            raise UnrecognizedDatabaseSchema(
                "unrecognized versioned Control Plane database schema; refusing to start"
            )
        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"] or ()),
                str(foreign_key["referred_table"]),
                tuple(foreign_key["referred_columns"] or ()),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        if actual_foreign_keys != expected_foreign_keys:
            raise UnrecognizedDatabaseSchema(
                "unrecognized versioned Control Plane database schema; refusing to start"
            )
        expected_indexes = {
            index.name: (tuple(column.name for column in index.columns), index.unique)
            for index in table.indexes
            if index.name is not None
        }
        actual_indexes = {
            index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table_name)
        }
        if any(
            actual_indexes.get(name) != signature
            for name, signature in expected_indexes.items()
        ):
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
