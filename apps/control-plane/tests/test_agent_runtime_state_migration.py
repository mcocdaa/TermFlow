"""Alembic coverage for the 0011 desired/observed runtime-state boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from termflow_control_plane.persistence import database as database_module
from termflow_control_plane.persistence import models
from termflow_control_plane.persistence.database import Database

_NOW = "2026-09-04 08:00:00"


def _migration_config(connection: Connection) -> Config:
    migration_path = files("termflow_control_plane.persistence").joinpath("migrations")
    config = Config()
    config.set_main_option("script_location", str(migration_path))
    config.set_main_option("sqlalchemy.url", str(connection.engine.url))
    config.attributes["connection"] = connection
    return config


@contextmanager
def _database(tmp_path: Path) -> Iterator[Connection]:
    engine = create_engine(f"sqlite:///{tmp_path / f'{uuid4().hex}.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        yield connection
    engine.dispose()


def _upgrade(connection: Connection, revision: str) -> None:
    if connection.in_transaction():
        connection.commit()
    command.upgrade(_migration_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    if connection.in_transaction():
        connection.commit()
    command.downgrade(_migration_config(connection), revision)


def _seed_profile_and_term(connection: Connection) -> tuple[str, str]:
    installation_id = uuid4().hex
    term_id = uuid4().hex
    profile_id = uuid4().hex
    connection.execute(
        text(
            """
            INSERT INTO installations (id, token_hash, created_at)
            VALUES (:id, :token_hash, :created_at)
            """
        ),
        {
            "id": installation_id,
            "token_hash": uuid4().hex + uuid4().hex,
            "created_at": _NOW,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO instances (
                id, installation_id, name, token_hash, created_at
            ) VALUES (
                :id, :installation_id, :name, :token_hash, :created_at
            )
            """
        ),
        {
            "id": term_id,
            "installation_id": installation_id,
            "name": f"term-{term_id[:8]}",
            "token_hash": uuid4().hex + uuid4().hex,
            "created_at": _NOW,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO agent_profiles (
                id, display_name, backend_kind, config, created_at, updated_at
            ) VALUES (
                :id, :display_name, 'opencode', :config, :created_at, :updated_at
            )
            """
        ),
        {
            "id": profile_id,
            "display_name": f"profile-{profile_id[:8]}",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
            "created_at": _NOW,
            "updated_at": _NOW,
        },
    )
    connection.commit()
    return profile_id, term_id


def _seed_binding(
    connection: Connection,
    *,
    profile_id: str,
    term_id: str,
    status: str,
    runtime_ref: str | None = None,
    runtime_epoch: int | None = None,
    capability_ref: str | None = None,
) -> str:
    binding_id = uuid4().hex
    connection.execute(
        text(
            """
            INSERT INTO agent_bindings (
                id, profile_id, term_id, status, runtime_ref, runtime_epoch,
                capability_ref, created_at, updated_at
            ) VALUES (
                :id, :profile_id, :term_id, :status, :runtime_ref,
                :runtime_epoch, :capability_ref, :created_at, :updated_at
            )
            """
        ),
        {
            "id": binding_id,
            "profile_id": profile_id,
            "term_id": term_id,
            "status": status,
            "runtime_ref": runtime_ref,
            "runtime_epoch": runtime_epoch,
            "capability_ref": capability_ref,
            "created_at": _NOW,
            "updated_at": _NOW,
        },
    )
    connection.commit()
    return binding_id


def _seed_runtime(
    connection: Connection,
    *,
    binding_id: str,
    runtime_ref: str,
    runtime_epoch: int,
    readiness: str,
    last_health_at: str | None = None,
) -> str:
    runtime_id = uuid4().hex
    connection.execute(
        text(
            """
            INSERT INTO agent_runtime_bindings (
                id, binding_id, runtime_ref, runtime_epoch, readiness,
                last_health_at, created_at, updated_at
            ) VALUES (
                :id, :binding_id, :runtime_ref, :runtime_epoch, :readiness,
                :last_health_at, :created_at, :updated_at
            )
            """
        ),
        {
            "id": runtime_id,
            "binding_id": binding_id,
            "runtime_ref": runtime_ref,
            "runtime_epoch": runtime_epoch,
            "readiness": readiness,
            "last_health_at": last_health_at,
            "created_at": _NOW,
            "updated_at": _NOW,
        },
    )
    connection.commit()
    return runtime_id


def _revision(connection: Connection) -> str:
    return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def test_0011_revision_leaves_sqlite_transaction_control_to_the_runner() -> None:
    migration_path = files("termflow_control_plane.persistence").joinpath(
        "migrations", "versions", "0011_agent_runtime_state.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert "dbapi_connection" not in source
    assert "PRAGMA foreign_keys" not in source


async def test_database_initialize_rolls_back_mid_0011_and_can_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atomic-0011.db"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        _upgrade(connection, "0010")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="ready",
            runtime_ref="desired-runtime",
            runtime_epoch=4,
            capability_ref="desired-capability",
        )
    engine.dispose()

    database = Database(f"sqlite+aiosqlite:///{database_path}")

    def fail_after_runtime_schema(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "create table agent_provider_disclosure_acceptances" in normalized:
            raise RuntimeError("injected failure after runtime schema changes")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail_after_runtime_schema)
    try:
        result = await database.initialize()
        assert result.agent_ready is False
        assert result.agent_reason_code == "recovery_failed"
    finally:
        event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            fail_after_runtime_schema,
        )

    try:
        async with database.engine.connect() as connection:
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0010"
            assert (
                await connection.execute(
                    text("SELECT status FROM agent_bindings WHERE id = :id"),
                    {"id": binding_id},
                )
            ).scalar_one() == "ready"
            binding_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("agent_bindings")
                }
            )
            runtime_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("agent_runtime_bindings")
                }
            )
            assert "config_revision" not in binding_columns
            assert "observed_runtime_ref" not in runtime_columns
            assert "runtime_ref" in runtime_columns

        await database.initialize()

        async with database.engine.connect() as connection:
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0014"
            assert (
                await connection.execute(
                    text("SELECT status FROM agent_bindings WHERE id = :id"),
                    {"id": binding_id},
                )
            ).scalar_one() == "enabled"
    finally:
        await database.dispose()


def test_0011_downgrade_failure_is_atomic_and_retryable(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="enabled",
            runtime_ref="desired-runtime",
            runtime_epoch=9,
            capability_ref="desired-capability",
        )
        token_id = uuid4().hex
        acceptance_id = uuid4().hex
        connection.execute(
            text(
                """
                INSERT INTO auth_tokens (
                    id, token_digest, kind, scopes, epoch, expires_at,
                    authenticated_at, created_at
                ) VALUES (
                    :id, :token_digest, 'access', '[]', 1, :expires_at,
                    :authenticated_at, :created_at
                )
                """
            ),
            {
                "id": token_id,
                "token_digest": uuid4().hex + uuid4().hex,
                "expires_at": "2026-09-05 08:00:00",
                "authenticated_at": _NOW,
                "created_at": _NOW,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO agent_provider_disclosure_acceptances (
                    id, binding_id, disclosure_fingerprint, provider_id,
                    model_id, endpoint_origin, region, retention_terms,
                    retention_version, no_training, policy_version,
                    accepted_at, accepted_auth_epoch, actor_kind, actor_ref
                ) VALUES (
                    :id, :binding_id, :fingerprint, 'deepseek',
                    'deepseek-v4-flash', 'https://api.deepseek.com', 'global',
                    'provider-policy', 'v1', 1, 'policy-v1', :accepted_at,
                    3, 'admin', 'root'
                )
                """
            ),
            {
                "id": acceptance_id,
                "binding_id": binding_id,
                "fingerprint": uuid4().hex + uuid4().hex,
                "accepted_at": _NOW,
            },
        )
        connection.commit()

        schema_before = connection.execute(
            text(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        ).all()
        data_before = {
            "binding": connection.execute(
                text("SELECT * FROM agent_bindings WHERE id = :id"),
                {"id": binding_id},
            ).one(),
            "token": connection.execute(
                text("SELECT * FROM auth_tokens WHERE id = :id"),
                {"id": token_id},
            ).one(),
            "acceptance": connection.execute(
                text(
                    """
                    SELECT * FROM agent_provider_disclosure_acceptances
                    WHERE id = :id
                    """
                ),
                {"id": acceptance_id},
            ).one(),
        }
        connection.commit()

        def fail_after_first_downgrade_schema_change(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if (
                "drop index "
                "ix_agent_provider_disclosure_acceptances_binding_disclosure_fingerprint"
                in normalized
            ):
                raise RuntimeError("injected failure after authenticated_at removal")

        event.listen(
            connection.engine,
            "before_cursor_execute",
            fail_after_first_downgrade_schema_change,
        )
        try:
            with pytest.raises(
                RuntimeError, match="injected failure after authenticated_at removal"
            ):
                _downgrade(connection, "0010")
        finally:
            event.remove(
                connection.engine,
                "before_cursor_execute",
                fail_after_first_downgrade_schema_change,
            )

        assert _revision(connection) == "0011"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        schema_after = connection.execute(
            text(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        ).all()
        assert schema_after == schema_before
        assert (
            connection.execute(
                text("SELECT * FROM agent_bindings WHERE id = :id"),
                {"id": binding_id},
            ).one()
            == data_before["binding"]
        )
        assert (
            connection.execute(
                text("SELECT * FROM auth_tokens WHERE id = :id"),
                {"id": token_id},
            ).one()
            == data_before["token"]
        )
        assert (
            connection.execute(
                text(
                    """
                SELECT * FROM agent_provider_disclosure_acceptances
                WHERE id = :id
                """
                ),
                {"id": acceptance_id},
            ).one()
            == data_before["acceptance"]
        )

        _downgrade(connection, "0010")
        assert _revision(connection) == "0010"
        assert "authenticated_at" not in {
            column["name"] for column in inspect(connection).get_columns("auth_tokens")
        }
        assert "agent_provider_disclosure_acceptances" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_unversioned_non_sqlite_upgrade_does_not_commit_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self) -> None:
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1

        def in_transaction(self) -> bool:
            return True

    class FakeInspector:
        def get_table_names(self) -> list[str]:
            return ["installations", "instances", "enrollment_tokens"]

    connection = FakeConnection()
    migration_config = SimpleNamespace(attributes={})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(database_module, "inspect", lambda _connection: FakeInspector())
    monkeypatch.setattr(
        database_module,
        "_prepare_unversioned_v2",
        lambda _connection: None,
    )
    monkeypatch.setattr(
        database_module,
        "_migration_config",
        lambda _connection: migration_config,
    )
    monkeypatch.setattr(
        database_module.command,
        "stamp",
        lambda _config, revision: calls.append(("stamp", revision)),
    )
    monkeypatch.setattr(
        database_module.command,
        "upgrade",
        lambda _config, revision: calls.append(("upgrade", revision)),
    )

    database_module._upgrade(connection)  # type: ignore[arg-type]

    assert connection.commit_calls == 0
    assert calls == [("stamp", "0001"), ("upgrade", "head")]


def test_0010_to_0011_maps_desired_without_inventing_observed_ready(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")

        pending_profile, pending_term = _seed_profile_and_term(connection)
        pending_id = _seed_binding(
            connection,
            profile_id=pending_profile,
            term_id=pending_term,
            status="pending",
        )
        disabled_profile, disabled_term = _seed_profile_and_term(connection)
        disabled_id = _seed_binding(
            connection,
            profile_id=disabled_profile,
            term_id=disabled_term,
            status="disabled",
        )
        revoked_profile, revoked_term = _seed_profile_and_term(connection)
        revoked_id = _seed_binding(
            connection,
            profile_id=revoked_profile,
            term_id=revoked_term,
            status="revoked",
        )
        ready_profile, ready_term = _seed_profile_and_term(connection)
        desired_ready_id = _seed_binding(
            connection,
            profile_id=ready_profile,
            term_id=ready_term,
            status="ready",
            runtime_ref="desired-only-runtime",
            runtime_epoch=8,
            capability_ref="desired-only-capability",
        )
        observed_profile, observed_term = _seed_profile_and_term(connection)
        observed_id = _seed_binding(
            connection,
            profile_id=observed_profile,
            term_id=observed_term,
            status="ready",
            runtime_ref="legacy-runtime",
            runtime_epoch=7,
            capability_ref="legacy-capability",
        )
        _seed_runtime(
            connection,
            binding_id=observed_id,
            runtime_ref="legacy-runtime",
            runtime_epoch=7,
            readiness="ready",
            last_health_at=_NOW,
        )

        _upgrade(connection, "0011")

        bindings = {
            row.id: row
            for row in connection.execute(
                text(
                    """
                    SELECT id, status, config_revision
                    FROM agent_bindings
                    """
                )
            ).mappings()
        }
        assert bindings[pending_id].status == "disabled"
        assert bindings[disabled_id].status == "disabled"
        assert bindings[revoked_id].status == "revoked"
        assert bindings[desired_ready_id].status == "enabled"
        assert bindings[observed_id].status == "enabled"
        assert all(row.config_revision == 1 for row in bindings.values())

        observed_rows = (
            connection.execute(
                text(
                    """
                SELECT binding_id, readiness, reason_code, observed_runtime_ref,
                       observed_runtime_epoch, observed_capability_ref,
                       applied_revision, config_fingerprint, last_health_at,
                       provider_readiness, provider_verified_revision,
                       provider_last_checked_at, provider_reason_code
                FROM agent_runtime_bindings
                """
                )
            )
            .mappings()
            .all()
        )
        assert len(observed_rows) == 1
        observed = observed_rows[0]
        assert observed.binding_id == observed_id
        assert observed.readiness == "not_ready"
        assert observed.reason_code == "legacy_revalidation_required"
        assert observed.observed_runtime_ref == "legacy-runtime"
        assert observed.observed_runtime_epoch == 7
        assert observed.observed_capability_ref is None
        assert observed.applied_revision is None
        assert observed.config_fingerprint is None
        assert str(observed.last_health_at).startswith("2026-09-04 08:00:00")
        assert observed.provider_readiness == "configured_unverified"
        assert observed.provider_verified_revision is None
        assert observed.provider_last_checked_at is None
        assert observed.provider_reason_code is None
        assert desired_ready_id not in {row.binding_id for row in observed_rows}


def test_0011_partial_unique_index_allows_only_one_non_revoked_binding(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "head")
        profile_id, term_id = _seed_profile_and_term(connection)
        _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="enabled",
        )

        with pytest.raises(IntegrityError):
            _seed_binding(
                connection,
                profile_id=profile_id,
                term_id=term_id,
                status="disabled",
            )
        connection.rollback()

        for _ in range(2):
            _seed_binding(
                connection,
                profile_id=profile_id,
                term_id=term_id,
                status="revoked",
            )
        statuses = (
            connection.execute(
                text(
                    """
                SELECT status FROM agent_bindings
                WHERE profile_id = :profile_id AND term_id = :term_id
                ORDER BY status
                """
                ),
                {"profile_id": profile_id, "term_id": term_id},
            )
            .scalars()
            .all()
        )
        assert statuses == ["enabled", "revoked", "revoked"]

        index_sql = connection.execute(
            text(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'index'
                  AND name = 'uq_agent_bindings_profile_term_active'
                """
            )
        ).scalar_one()
        assert "UNIQUE INDEX" in index_sql.upper()
        assert "WHERE status != 'revoked'" in index_sql


def test_0011_round_trip_preserves_runtime_refs_and_epochs(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="ready",
            runtime_ref="desired-runtime",
            runtime_epoch=17,
            capability_ref="desired-capability",
        )
        runtime_id = _seed_runtime(
            connection,
            binding_id=binding_id,
            runtime_ref="observed-runtime",
            runtime_epoch=16,
            readiness="ready",
            last_health_at=_NOW,
        )

        _upgrade(connection, "0011")
        _downgrade(connection, "0010")

        assert _revision(connection) == "0010"
        binding = (
            connection.execute(
                text(
                    """
                SELECT status, runtime_ref, runtime_epoch, capability_ref
                FROM agent_bindings WHERE id = :id
                """
                ),
                {"id": binding_id},
            )
            .mappings()
            .one()
        )
        assert dict(binding) == {
            "status": "ready",
            "runtime_ref": "desired-runtime",
            "runtime_epoch": 17,
            "capability_ref": "desired-capability",
        }
        runtime = (
            connection.execute(
                text(
                    """
                SELECT id, binding_id, runtime_ref, runtime_epoch, readiness,
                       last_health_at
                FROM agent_runtime_bindings WHERE id = :id
                """
                ),
                {"id": runtime_id},
            )
            .mappings()
            .one()
        )
        assert runtime.binding_id == binding_id
        assert runtime.runtime_ref == "observed-runtime"
        assert runtime.runtime_epoch == 16
        assert runtime.readiness == "not_ready"
        assert str(runtime.last_health_at).startswith("2026-09-04 08:00:00")
        assert {
            column["name"] for column in inspect(connection).get_columns("agent_runtime_bindings")
        } == {
            "id",
            "binding_id",
            "runtime_ref",
            "runtime_epoch",
            "readiness",
            "last_health_at",
            "created_at",
            "updated_at",
        }


@pytest.mark.parametrize("unknown_status", ["failed", "deleted", "enabled"])
def test_0011_upgrade_rejects_unknown_0010_binding_status_without_mutation(
    tmp_path: Path,
    unknown_status: str,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status=unknown_status,
        )

        with pytest.raises(RuntimeError, match="unknown agent_bindings status"):
            _upgrade(connection, "0011")
        connection.rollback()

        assert _revision(connection) == "0010"
        assert (
            connection.execute(
                text("SELECT status FROM agent_bindings WHERE id = :id"),
                {"id": binding_id},
            ).scalar_one()
            == unknown_status
        )
        assert "config_revision" not in {
            column["name"] for column in inspect(connection).get_columns("agent_bindings")
        }


def test_0011_upgrade_rejects_multiple_non_revoked_bindings_without_mutation(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")
        profile_id, term_id = _seed_profile_and_term(connection)
        first_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="pending",
        )
        second_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="ready",
        )

        with pytest.raises(RuntimeError, match="multiple non-revoked"):
            _upgrade(connection, "0011")
        connection.rollback()

        assert _revision(connection) == "0010"
        statuses = connection.execute(
            text("SELECT id, status FROM agent_bindings ORDER BY id")
        ).all()
        assert set(statuses) == {(first_id, "pending"), (second_id, "ready")}


def test_0011_upgrade_rejects_multiple_runtime_rows_per_binding_without_mutation(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="ready",
        )
        _seed_runtime(
            connection,
            binding_id=binding_id,
            runtime_ref="runtime-a",
            runtime_epoch=1,
            readiness="not_ready",
        )
        _seed_runtime(
            connection,
            binding_id=binding_id,
            runtime_ref="runtime-b",
            runtime_epoch=1,
            readiness="not_ready",
        )

        with pytest.raises(RuntimeError, match="multiple agent_runtime_bindings"):
            _upgrade(connection, "0011")
        connection.rollback()

        assert _revision(connection) == "0010"
        assert (
            connection.execute(
                text("SELECT count(*) FROM agent_runtime_bindings WHERE binding_id = :id"),
                {"id": binding_id},
            ).scalar_one()
            == 2
        )


def test_0011_fresh_database_has_exact_runtime_disclosure_and_auth_schema(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        assert _revision(connection) == "0011"
        inspector = inspect(connection)

        expected_columns = {
            "agent_bindings": {
                "id",
                "profile_id",
                "term_id",
                "status",
                "runtime_ref",
                "runtime_epoch",
                "capability_ref",
                "config_revision",
                "created_at",
                "updated_at",
            },
            "agent_runtime_bindings": {
                "id",
                "binding_id",
                "readiness",
                "reason_code",
                "observed_runtime_ref",
                "observed_runtime_epoch",
                "observed_capability_ref",
                "applied_revision",
                "config_fingerprint",
                "last_health_at",
                "transition_started_at",
                "provider_readiness",
                "provider_verified_revision",
                "provider_last_checked_at",
                "provider_reason_code",
                "created_at",
                "updated_at",
            },
            "agent_provider_disclosure_acceptances": {
                "id",
                "binding_id",
                "disclosure_fingerprint",
                "provider_id",
                "model_id",
                "endpoint_origin",
                "region",
                "retention_terms",
                "retention_version",
                "no_training",
                "policy_version",
                "accepted_at",
                "accepted_auth_epoch",
                "actor_kind",
                "actor_ref",
                "revoked_at",
            },
        }
        for table_name, columns in expected_columns.items():
            assert {column["name"] for column in inspector.get_columns(table_name)} == columns
            metadata_columns = set(models.Base.metadata.tables[table_name].columns.keys())
            if table_name == "agent_bindings":
                # Added by the post-0011 write-policy revision; this snapshot
                # pins the legacy 0011 schema.
                metadata_columns -= {"write_policy"}
            assert metadata_columns == columns

        disclosure_columns = expected_columns["agent_provider_disclosure_acceptances"]
        assert not any(
            forbidden in column
            for column in disclosure_columns
            for forbidden in ("credential", "secret", "api_key", "token")
        )
        auth_columns = {column["name"]: column for column in inspector.get_columns("auth_tokens")}
        assert auth_columns["authenticated_at"]["nullable"] is True
        assert models.AuthToken.__table__.c.authenticated_at.nullable is True

        runtime_unique = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("agent_runtime_bindings")
        }
        assert ("binding_id",) in runtime_unique
        assert ("observed_runtime_ref", "observed_runtime_epoch") in runtime_unique

        runtime_indexes = {
            index["name"] for index in inspector.get_indexes("agent_runtime_bindings")
        }
        disclosure_indexes = {
            index["name"]
            for index in inspector.get_indexes("agent_provider_disclosure_acceptances")
        }
        assert "ix_agent_runtime_bindings_readiness_last_health_at" in runtime_indexes
        assert "ix_agent_runtime_bindings_provider_readiness_last_checked_at" in runtime_indexes
        assert (
            "ix_agent_provider_disclosure_acceptances_binding_disclosure_fingerprint"
            in disclosure_indexes
        )


def test_0011_existing_auth_tokens_have_unknown_authentication_time(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0010")
        token_id = uuid4().hex
        connection.execute(
            text(
                """
                INSERT INTO auth_tokens (
                    id, token_digest, kind, scopes, epoch, expires_at, created_at
                ) VALUES (
                    :id, :token_digest, 'access', '[]', 1, :expires_at, :created_at
                )
                """
            ),
            {
                "id": token_id,
                "token_digest": uuid4().hex + uuid4().hex,
                "expires_at": "2026-09-05 08:00:00",
                "created_at": _NOW,
            },
        )
        connection.commit()

        _upgrade(connection, "0011")

        assert (
            connection.execute(
                text("SELECT authenticated_at FROM auth_tokens WHERE id = :id"),
                {"id": token_id},
            ).scalar_one()
            is None
        )


def test_0011_disclosure_acceptance_is_history_without_credentials_and_cascades(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        profile_id, term_id = _seed_profile_and_term(connection)
        binding_id = _seed_binding(
            connection,
            profile_id=profile_id,
            term_id=term_id,
            status="disabled",
        )
        acceptance_ids: list[str] = []
        for version in ("v1", "v2"):
            acceptance_id = uuid4().hex
            acceptance_ids.append(acceptance_id)
            connection.execute(
                text(
                    """
                    INSERT INTO agent_provider_disclosure_acceptances (
                        id, binding_id, disclosure_fingerprint, provider_id, model_id,
                        endpoint_origin, region, retention_terms,
                        retention_version, no_training, policy_version,
                        accepted_at, accepted_auth_epoch, actor_kind, actor_ref,
                        revoked_at
                    ) VALUES (
                        :id, :binding_id, :disclosure_fingerprint, 'deepseek',
                        'deepseek-v4-flash', 'https://api.deepseek.com',
                        'global', 'provider-policy', :retention_version, 1,
                        'policy-v1', :accepted_at, 4, 'admin', 'root', NULL
                    )
                    """
                ),
                {
                    "id": acceptance_id,
                    "binding_id": binding_id,
                    "disclosure_fingerprint": uuid4().hex + uuid4().hex,
                    "retention_version": version,
                    "accepted_at": _NOW,
                },
            )
        connection.commit()
        assert (
            connection.execute(
                text(
                    """
                SELECT count(*) FROM agent_provider_disclosure_acceptances
                WHERE binding_id = :binding_id
                """
                ),
                {"binding_id": binding_id},
            ).scalar_one()
            == 2
        )

        connection.execute(text("DELETE FROM agent_bindings WHERE id = :id"), {"id": binding_id})
        connection.commit()
        assert (
            connection.execute(
                text("SELECT count(*) FROM agent_provider_disclosure_acceptances")
            ).scalar_one()
            == 0
        )


def test_0011_downgrade_rejects_unrepresentable_revoked_history_without_mutation(
    tmp_path: Path,
) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        profile_id, term_id = _seed_profile_and_term(connection)
        revoked_ids = {
            _seed_binding(
                connection,
                profile_id=profile_id,
                term_id=term_id,
                status="revoked",
            )
            for _ in range(2)
        }

        with pytest.raises(RuntimeError, match="revoked history"):
            _downgrade(connection, "0010")
        connection.rollback()

        assert _revision(connection) == "0011"
        assert (
            set(
                connection.execute(
                    text(
                        """
                    SELECT id FROM agent_bindings
                    WHERE profile_id = :profile_id AND term_id = :term_id
                    """
                    ),
                    {"profile_id": profile_id, "term_id": term_id},
                ).scalars()
            )
            == revoked_ids
        )
