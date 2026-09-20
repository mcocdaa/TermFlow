"""Async database lifecycle with fail-closed, packaged Alembic upgrades."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import Table, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import AuditEvent, Base, EnrollmentToken, Installation, Instance

logger = logging.getLogger(__name__)


class UnrecognizedDatabaseSchema(RuntimeError):
    """Raised instead of guessing how to mutate an unknown unversioned database."""


_CORE_TABLES: dict[str, Table] = {
    "enrollment_tokens": cast(Table, EnrollmentToken.__table__),
    "installations": cast(Table, Installation.__table__),
    "instances": cast(Table, Instance.__table__),
    "audit_events": cast(Table, AuditEvent.__table__),
}
# Revisions through 0005 are owned by the core product.  Agent migrations are
# deliberately run as a second stage so a bad/temporarily unavailable Agent
# migration cannot take the terminal and Web C process down with it.
_CORE_REVISION = "0005"
_CORE_SCHEMA_TABLES = frozenset(
    {
        "enrollment_tokens",
        "installations",
        "instances",
        "audit_events",
        "authentication_state",
        "totp_setups",
        "auth_challenges",
        "native_clients",
        "oauth_authorizations",
        "auth_tokens",
        "auth_audit_events",
    }
)
# 0011 adds this one column to a core-owned table.  It is allowed when the
# database is already past the Agent stage, but is not required for the core
# 0005 validation itself.
_POST_CORE_COLUMNS = {"auth_tokens": frozenset({"authenticated_at"})}
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


@dataclass(frozen=True, slots=True)
class DatabaseInitializationResult:
    """Outcome of the two-stage database startup.

    Core migration/integrity failures still raise and prevent an unsafe
    process from starting.  Agent-stage failures are returned as a degraded
    result so callers can keep the core terminal surface available and fence
    Agent routes/work until a later retry succeeds.
    """

    agent_ready: bool = True
    agent_reason_code: str | None = None


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
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
            )

    v2_instance_columns = {column["name"] for column in inspector.get_columns("instances")}
    if set(_V2_ADDITIONS["instances"]) <= v2_instance_columns:
        ownership_foreign_keys = inspector.get_foreign_keys("instances")
        if ownership_foreign_keys and not any(
            foreign_key.get("referred_table") == "installations"
            and foreign_key.get("constrained_columns") == ["installation_id"]
            for foreign_key in ownership_foreign_keys
        ):
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
            )
    for table_name in _REQUIRED_UNVERSIONED_TABLES:
        duplicate = connection.execute(
            text(f"SELECT 1 FROM {table_name} GROUP BY token_hash HAVING COUNT(*) > 1 LIMIT 1")
        ).first()
        if duplicate is not None:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
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
        connection.execute(text("ALTER TABLE instances__termflow_v2 RENAME TO instances"))
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
        inspected_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
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
            actual_indexes.get(name) != signature for name, signature in expected_indexes.items()
        ):
            raise UnrecognizedDatabaseSchema(
                "unrecognized versioned Control Plane database schema; refusing to start"
            )
    state_rows = connection.execute(text("SELECT id, epoch FROM authentication_state")).all()
    if len(state_rows) != 1 or state_rows[0][0] != 1 or state_rows[0][1] < 1:
        raise UnrecognizedDatabaseSchema("unrecognized authentication state; refusing to start")


def _validate_core_schema(connection: Connection) -> None:
    """Validate the core schema at the 0005 boundary.

    The full metadata contains Agent tables and the post-0005
    ``auth_tokens.authenticated_at`` column, so the strict head validator
    cannot be used between the two migration stages.  Core validation is
    intentionally strict for the tables/columns it owns while allowing the
    known Agent-owned tables and the one later auth column to remain present
    on an already-upgraded database.
    """

    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    expected_tables = _CORE_SCHEMA_TABLES | {"alembic_version"}
    if not expected_tables <= actual_tables:
        raise UnrecognizedDatabaseSchema(
            "unrecognized core Control Plane database schema; refusing to start"
        )

    for table_name in _CORE_SCHEMA_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is None:  # pragma: no cover - a packaging/programming error
            raise UnrecognizedDatabaseSchema(
                "core schema metadata is incomplete; refusing to start"
            )
        inspected_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in table.columns}
        optional_later = set(_POST_CORE_COLUMNS.get(table_name, ()))
        required_columns = expected_columns - optional_later
        if not required_columns <= set(inspected_columns) <= expected_columns:
            raise UnrecognizedDatabaseSchema(
                "unrecognized core Control Plane database schema; refusing to start"
            )
        for column in table.columns:
            if column.name in optional_later and column.name not in inspected_columns:
                continue
            inspected_column = inspected_columns[column.name]
            if str(inspected_column["type"]).upper() != str(column.type).upper():
                raise UnrecognizedDatabaseSchema(
                    "unrecognized core Control Plane database schema; refusing to start"
                )
            if not column.primary_key and inspected_column["nullable"] != column.nullable:
                raise UnrecognizedDatabaseSchema(
                    "unrecognized core Control Plane database schema; refusing to start"
                )
        primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns")
        expected_primary_key = [column.name for column in table.primary_key.columns]
        if primary_key != expected_primary_key:
            raise UnrecognizedDatabaseSchema(
                "unrecognized core Control Plane database schema; refusing to start"
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
                "unrecognized core Control Plane database schema; refusing to start"
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
            actual_indexes.get(name) != signature for name, signature in expected_indexes.items()
        ):
            raise UnrecognizedDatabaseSchema(
                "unrecognized core Control Plane database schema; refusing to start"
            )

    state_rows = connection.execute(text("SELECT id, epoch FROM authentication_state")).all()
    if len(state_rows) != 1 or state_rows[0][0] != 1 or state_rows[0][1] < 1:
        raise UnrecognizedDatabaseSchema("unrecognized authentication state; refusing to start")


def _upgrade(
    connection: Connection,
    target: str = "head",
    *,
    validator: Any | None = None,
) -> None:
    table_names = set(inspect(connection).get_table_names())
    config = _migration_config(connection)
    if table_names and "alembic_version" not in table_names:
        try:
            _prepare_unversioned_v2(connection)
        except IntegrityError as exc:
            raise UnrecognizedDatabaseSchema(
                "unrecognized unversioned Control Plane database schema; refusing automatic upgrade"
            ) from exc
        if connection.dialect.name == "sqlite":
            connection.commit()
        command.stamp(config, "0001")
    elif connection.dialect.name == "sqlite" and connection.in_transaction():
        # SQLite's Alembic runner must toggle foreign-key enforcement before
        # opening its DDL transaction.  Schema inspection creates a SQLAlchemy
        # autobegin transaction even though it only reads metadata.
        connection.commit()
    config.attributes["post_migration_validate"] = validator or (
        _validate_core_schema if target == _CORE_REVISION else _validate_head_schema
    )
    command.upgrade(config, target)


def _upgrade_core(connection: Connection) -> None:
    """Apply and validate only the core migration chain (0001 through 0005)."""

    _upgrade(connection, _CORE_REVISION, validator=_validate_core_schema)


def _upgrade_agent(connection: Connection) -> None:
    """Apply and validate the Agent-owned chain after core is healthy."""

    _upgrade(connection, "head", validator=_validate_head_schema)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        if self.engine.url.get_backend_name() == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_foreign_keys(connection: Any, _record: Any) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self) -> DatabaseInitializationResult:
        """Initialize core first, then attempt the Agent migration stage.

        The stages intentionally have separate transaction boundaries.  A
        failed Agent migration is rolled back by Alembic and reported to the
        composition root; the already-validated core schema remains usable and
        the next process/retry can attempt the Agent chain again.
        """

        is_sqlite = self.engine.url.get_backend_name() == "sqlite"
        if is_sqlite:
            database_path = self.engine.url.database
            if database_path and database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
            async with self.engine.connect() as connection:
                await connection.execute(text("PRAGMA journal_mode=WAL"))
                await connection.execute(text("PRAGMA foreign_keys=ON"))
                # End the SQLAlchemy autobegin transaction created by PRAGMA
                # statements before Alembic takes ownership of migration
                # transaction and temporary foreign-key mode.
                await connection.commit()
                await connection.run_sync(_upgrade_core)
                try:
                    await connection.run_sync(_upgrade_agent)
                except Exception:
                    # Alembic's SQLite runner has already rolled back the
                    # Agent transaction before propagating the exception.
                    logger.exception("Agent migration stage failed; core remains available")
                    return DatabaseInitializationResult(
                        agent_ready=False,
                        agent_reason_code="recovery_failed",
                    )
            return DatabaseInitializationResult()

        # Keep the non-SQLite stages in separate engine transactions as well;
        # a failed transaction cannot be safely committed after an exception.
        async with self.engine.begin() as connection:
            await connection.run_sync(_upgrade_core)
        try:
            async with self.engine.begin() as connection:
                await connection.run_sync(_upgrade_agent)
        except Exception:
            logger.exception("Agent migration stage failed; core remains available")
            return DatabaseInitializationResult(
                agent_ready=False,
                agent_reason_code="recovery_failed",
            )
        return DatabaseInitializationResult()

    async def dispose(self) -> None:
        await self.engine.dispose()
