from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from termflow_control_plane.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def _post_migration_validate(connection: Connection) -> None:
    callback = config.attributes.get("post_migration_validate")
    if callback is not None:
        if not callable(callback):
            raise TypeError("post_migration_validate must be callable")
        callback(connection)


def _set_sqlite_foreign_keys(connection: Connection, *, enabled: bool) -> None:
    if connection.in_transaction():
        raise RuntimeError("SQLite foreign-key mode must be changed outside a transaction")
    expected = 1 if enabled else 0
    connection.exec_driver_sql(f"PRAGMA foreign_keys={expected}")
    connection.commit()
    actual = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    connection.commit()
    if actual != expected:
        raise RuntimeError("could not set SQLite foreign-key enforcement mode")


def _run_sqlite(connection: Connection) -> None:
    if connection.in_transaction():
        raise RuntimeError(
            "SQLite Alembic runner requires a connection without an active transaction"
        )
    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    connection.commit()
    _set_sqlite_foreign_keys(connection, enabled=False)
    try:
        # Python's sqlite3 legacy transaction mode does not emit BEGIN for DDL.
        # Start a physical transaction before Alembic configures its context;
        # Alembic then detects an externally-owned transaction and cannot
        # commit between revision operations.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=True,
                transactional_ddl=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                _post_migration_validate(connection)
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(1)
                if violations:
                    raise RuntimeError(
                        f"SQLite migration introduced a foreign-key violation: {violations[0]!r}"
                    )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        if connection.in_transaction():
            connection.rollback()
        _set_sqlite_foreign_keys(connection, enabled=foreign_keys_enabled)


def _run(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        _run_sqlite(connection)
        return
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()
        _post_migration_validate(connection)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if isinstance(supplied, Connection):
        _run(supplied)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
