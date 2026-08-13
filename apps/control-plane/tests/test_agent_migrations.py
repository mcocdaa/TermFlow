"""Exact-schema and lifecycle tests for the Agent Broker migration (plan §15; task M1.1).

These tests exercise the packaged Alembic chain: a fresh database migrates to
the new head, an existing ``0005`` database upgrades, the head downgrades back
to ``0005``, the migrated schema exactly matches the ORM metadata, the head
schema validation passes on an already-migrated database, and Term deletion
cascades to Agent rows through real foreign keys.
"""

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from termflow_control_plane.persistence.database import Database, _migration_config
from termflow_control_plane.persistence.models import Base

# The migration head after this task.  Only tests asserting the GLOBAL head use
# this constant; upgrade-path fixtures still pin "0005" explicitly.
HEAD = "0009"

# Every Agent Broker table created by migrations 0006, 0007, 0008, and 0009 (plan §15).
AGENT_TABLES = (
    "agent_profiles",
    "agent_bindings",
    "agent_memory_scopes",
    "agent_conversations",
    "agent_backend_conversations",
    "agent_runtime_bindings",
    "agent_inbox_items",
    "agent_tool_requests",
    "agent_runs",
    "agent_messages",
    "agent_events",
    "agent_tokens",
    "pane_policies",
    "approval_requests",
    "approval_audit_events",
    "write_grants",
    "watches",
    "watch_deliveries",
    "transcript_drafts",
    "agent_cleanup_jobs",
    "agent_diagnostics",
    "pane_observation_cursors",
)

AGENT_TABLE_SET = set(AGENT_TABLES)

# Tables that must still exist after a downgrade to "0005".
PRE_AGENT_TABLES = {
    "alembic_version",
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


def _table_names(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _foreign_key_signatures(
    inspector, table_name: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], tuple[tuple[str, str], ...]]]:
    return {
        (
            tuple(foreign_key["constrained_columns"] or ()),
            str(foreign_key["referred_table"]),
            tuple(foreign_key["referred_columns"] or ()),
            tuple(sorted((foreign_key.get("options") or {}).items())),
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    }


def _unique_signatures(inspector, table_name: str) -> set[frozenset[str]]:
    return {
        frozenset(constraint["column_names"] or ())
        for constraint in inspector.get_unique_constraints(table_name)
    }


def _index_signatures(
    inspector, table_name: str
) -> dict[str, tuple[tuple[str, ...], bool]]:
    return {
        index["name"]: (tuple(index["column_names"] or ()), bool(index["unique"]))
        for index in inspector.get_indexes(table_name)
        if not str(index["name"]).startswith("sqlite_autoindex")
    }


def test_fresh_database_migrates_to_agent_broker_head(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh-agent.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "head")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == HEAD
        assert AGENT_TABLE_SET <= table_names
    finally:
        engine.dispose()


def test_upgrade_from_0005_to_head_creates_agent_tables(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade-agent.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "0005")
            assert AGENT_TABLE_SET.isdisjoint(_table_names(connection))

            command.upgrade(config, "head")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert AGENT_TABLE_SET <= table_names
        assert revision == HEAD
    finally:
        engine.dispose()


def test_upgrade_from_0006_to_head_adds_watch_engine_schema(tmp_path) -> None:
    """The 0007 delta is additive on an 0006 database (plan §11.2)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade-0006.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "0006")
            assert "pane_observation_cursors" not in _table_names(connection)
            watch_columns = {
                column["name"] for column in inspect(connection).get_columns("watches")
            }
            assert "matcher_state" not in watch_columns

            command.upgrade(config, "head")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            watch_columns = {
                column["name"] for column in inspect(connection).get_columns("watches")
            }
        assert revision == HEAD
        assert "pane_observation_cursors" in table_names
        assert "matcher_state" in watch_columns
    finally:
        engine.dispose()


def test_watch_engine_schema_delta_columns_and_constraints(tmp_path) -> None:
    """The per-pane cursor ledger has its exact columns and unique pane key."""
    engine = create_engine(f"sqlite:///{tmp_path / 'watch-schema.db'}")
    try:
        with engine.begin() as connection:
            command.upgrade(_migration_config(connection), "head")
        inspector = inspect(engine)

        columns = {
            column["name"]: column for column in inspector.get_columns(
                "pane_observation_cursors"
            )
        }
        assert set(columns) == {
            "instance_id",
            "pane_id",
            "pane_incarnation",
            "stream_id",
            "seq",
            "observed_at",
            "updated_at",
        }
        assert not columns["pane_incarnation"]["nullable"]
        assert not columns["seq"]["nullable"]

        primary_key = inspector.get_pk_constraint("pane_observation_cursors")
        assert primary_key["constrained_columns"] == ["instance_id", "pane_id"]

        matcher_state = {
            column["name"]: column
            for column in inspector.get_columns("watches")
        }["matcher_state"]
        assert matcher_state["nullable"] is True
    finally:
        engine.dispose()


def test_downgrade_from_head_to_0005_drops_agent_tables(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade-agent.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "head")
            assert AGENT_TABLE_SET <= _table_names(connection)

            command.downgrade(config, "0005")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert AGENT_TABLE_SET.isdisjoint(table_names)
        assert PRE_AGENT_TABLES <= table_names
        assert revision == "0005"
    finally:
        engine.dispose()


def test_downgrade_from_head_to_0006_drops_watch_engine_schema(tmp_path) -> None:
    """The 0007 delta downgrades cleanly back to the M1 schema."""
    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade-0006.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "head")
            assert "pane_observation_cursors" in _table_names(connection)

            command.downgrade(config, "0006")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            watch_columns = {
                column["name"] for column in inspect(connection).get_columns("watches")
            }
        assert revision == "0006"
        assert "pane_observation_cursors" not in table_names
        assert "matcher_state" not in watch_columns
    finally:
        engine.dispose()


def test_upgrade_from_0007_to_head_adds_approval_audit_schema(tmp_path) -> None:
    """The 0008 delta is additive on an 0007 database (plan §12.1, M5.2)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade-0007.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "0007")
            assert "approval_audit_events" not in _table_names(connection)
            approval_columns = {
                column["name"] for column in inspect(connection).get_columns(
                    "approval_requests"
                )
            }
            assert "pane_id" not in approval_columns

            command.upgrade(config, "head")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            approval_columns = {
                column["name"] for column in inspect(connection).get_columns(
                    "approval_requests"
                )
            }
        assert revision == HEAD
        assert "approval_audit_events" in table_names
        assert {"pane_id", "operation", "intent_summary"} <= approval_columns
    finally:
        engine.dispose()


def test_approval_audit_schema_delta_columns_and_indexes(tmp_path) -> None:
    """The audit table has its exact columns, FKs, and indexes (spec §7)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-schema.db'}")
    try:
        with engine.begin() as connection:
            command.upgrade(_migration_config(connection), "head")
        inspector = inspect(engine)

        columns = {
            column["name"]: column
            for column in inspector.get_columns("approval_audit_events")
        }
        assert set(columns) == {
            "id",
            "created_at",
            "event_type",
            "approval_id",
            "binding_id",
            "instance_id",
            "runtime_epoch",
            "conversation_id",
            "run_id",
            "tool_call_id",
            "pane_id",
            "operation",
            "input_bytes",
            "canonical_hash",
            "auth_epoch",
            "actor",
            "outcome",
            "error_code",
        }
        # Metadata-only: no raw text/keys columns exist.
        assert "text" not in columns
        assert "keys" not in columns

        foreign_keys = _foreign_key_signatures(inspector, "approval_audit_events")
        assert (("approval_id",), "approval_requests", ("id",)) in {
            (fk[0], fk[1], fk[2]) for fk in foreign_keys
        }
        assert (("binding_id",), "agent_bindings", ("id",)) in {
            (fk[0], fk[1], fk[2]) for fk in foreign_keys
        }
        assert (("conversation_id",), "agent_conversations", ("id",)) in {
            (fk[0], fk[1], fk[2]) for fk in foreign_keys
        }

        indexes = _index_signatures(inspector, "approval_audit_events")
        assert indexes["ix_approval_audit_events_approval_id"] == (("approval_id",), False)
        assert indexes["ix_approval_audit_events_created_at"] == (("created_at",), False)
    finally:
        engine.dispose()


def test_downgrade_from_head_to_0007_drops_approval_audit_schema(tmp_path) -> None:
    """The 0008 delta downgrades cleanly back to the M3 schema."""
    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade-0007.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "head")
            assert "approval_audit_events" in _table_names(connection)

            command.downgrade(config, "0007")
            table_names = _table_names(connection)
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            approval_columns = {
                column["name"] for column in inspect(connection).get_columns(
                    "approval_requests"
                )
            }
        assert revision == "0007"
        assert "approval_audit_events" not in table_names
        assert "pane_id" not in approval_columns
        assert "intent_summary" not in approval_columns
    finally:
        engine.dispose()


def test_agent_event_payload_column_delta(tmp_path) -> None:
    """The 0009 delta adds the nullable payload column and drops it cleanly
    (plan M6a spec §5)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'payload-delta.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "0007")
            columns = {
                column["name"] for column in inspect(connection).get_columns("agent_events")
            }
            assert "payload" not in columns

            command.upgrade(config, "head")
            payload = {
                column["name"]: column
                for column in inspect(connection).get_columns("agent_events")
            }["payload"]
        assert payload["nullable"] is True
        assert str(payload["type"]).upper() == "TEXT"

        with engine.begin() as connection:
            command.downgrade(_migration_config(connection), "0007")
            columns = {
                column["name"] for column in inspect(connection).get_columns("agent_events")
            }
        assert "payload" not in columns
    finally:
        engine.dispose()


def test_migrated_schema_matches_orm_metadata_exactly(tmp_path) -> None:
    orm_engine = create_engine(f"sqlite:///{tmp_path / 'orm-agent.db'}")
    migrated_engine = create_engine(f"sqlite:///{tmp_path / 'migrated-agent.db'}")
    try:
        Base.metadata.create_all(orm_engine)
        with migrated_engine.begin() as connection:
            command.upgrade(_migration_config(connection), "head")

        orm_inspector = inspect(orm_engine)
        migrated_inspector = inspect(migrated_engine)
        for table_name in AGENT_TABLES:
            orm_columns = {
                column["name"]: column for column in orm_inspector.get_columns(table_name)
            }
            migrated_columns = {
                column["name"]: column
                for column in migrated_inspector.get_columns(table_name)
            }
            assert set(orm_columns) == set(migrated_columns), (
                f"{table_name}: column sets differ"
            )
            for name in orm_columns:
                assert str(orm_columns[name]["type"]).upper() == str(
                    migrated_columns[name]["type"]
                ).upper(), f"{table_name}.{name}: type differs"
                assert orm_columns[name]["nullable"] == migrated_columns[name]["nullable"], (
                    f"{table_name}.{name}: nullability differs"
                )
            assert (
                orm_inspector.get_pk_constraint(table_name)["constrained_columns"]
                == migrated_inspector.get_pk_constraint(table_name)["constrained_columns"]
            ), f"{table_name}: primary key differs"
            assert _foreign_key_signatures(
                orm_inspector, table_name
            ) == _foreign_key_signatures(
                migrated_inspector, table_name
            ), f"{table_name}: foreign keys (incl. ondelete) differ"
            assert _unique_signatures(orm_inspector, table_name) == _unique_signatures(
                migrated_inspector, table_name
            ), f"{table_name}: unique constraints differ"
            assert _index_signatures(
                orm_inspector, table_name
            ) == _index_signatures(
                migrated_inspector, table_name
            ), f"{table_name}: indexes differ"
    finally:
        orm_engine.dispose()
        migrated_engine.dispose()


def test_key_agent_indexes_and_unique_constraints_are_created(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'key-indexes-agent.db'}")
    try:
        with engine.begin() as connection:
            command.upgrade(_migration_config(connection), "head")
        inspector = inspect(engine)

        inbox_claims = {
            tuple(index["column_names"] or ())
            for index in inspector.get_indexes("agent_inbox_items")
            if index["name"] == "ix_agent_inbox_items_claims"
        }
        assert inbox_claims == {("delivery_state", "next_attempt_at", "claim_expires_at")}

        watch_delivery_indexes = {
            index["name"] for index in inspector.get_indexes("watch_deliveries")
        }
        assert "ix_watch_deliveries_next_attempt_at" in watch_delivery_indexes

        inbox_uniques = {
            tuple(constraint["column_names"] or ())
            for constraint in inspector.get_unique_constraints("agent_inbox_items")
        }
        assert ("conversation_id", "admission_seq") in inbox_uniques

        approval_uniques = {
            tuple(constraint["column_names"] or ())
            for constraint in inspector.get_unique_constraints("approval_requests")
        }
        assert ("conversation_id", "tool_call_id") in approval_uniques
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_initialize_on_migrated_database_passes_head_schema_validation(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'head-valid-agent.db'}")
    await database.initialize()
    try:
        # A second initialize must not raise UnrecognizedDatabaseSchema: the
        # live table set exactly matches Base.metadata at the new head.
        await database.initialize()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_fresh_database_initialize_reports_agent_broker_head(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh-head-agent.db'}")
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            table_names = set(
                await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            )
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == HEAD
        assert AGENT_TABLE_SET <= table_names
    finally:
        await database.dispose()


def test_deleting_term_cascades_to_agent_bindings(tmp_path) -> None:
    path = tmp_path / "cascade-agent.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            command.upgrade(_migration_config(connection), "head")
    finally:
        engine.dispose()

    installation_id = uuid4()
    instance_id = uuid4()
    profile_id = uuid4()
    binding_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO installations (id, token_hash, created_at) VALUES (?, ?, ?)",
            (str(installation_id), uuid4().hex, now),
        )
        connection.execute(
            "INSERT INTO instances (id, installation_id, name, token_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(instance_id), str(installation_id), "term", uuid4().hex, now),
        )
        connection.execute(
            "INSERT INTO agent_profiles (id, display_name, backend_kind, config, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(profile_id), "Assistant", "opencode", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO agent_bindings (id, profile_id, term_id, status, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(binding_id), str(profile_id), str(instance_id), "active", now, now),
        )
        assert connection.execute("SELECT COUNT(*) FROM agent_bindings").fetchone()[0] == 1

        connection.execute("DELETE FROM instances WHERE id = ?", (str(instance_id),))
        remaining = connection.execute("SELECT COUNT(*) FROM agent_bindings").fetchone()[0]
        connection.commit()
    assert remaining == 0
