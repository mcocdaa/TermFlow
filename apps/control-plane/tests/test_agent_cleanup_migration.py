"""Migration and schema contracts for cleanup manifests (v0.2.0 Task 1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(files("termflow_control_plane.persistence").joinpath("migrations")),
    )
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
    command.upgrade(_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    if connection.in_transaction():
        connection.commit()
    command.downgrade(_config(connection), revision)


def _seed_term_and_job(connection: Connection, *, state: str = "pending") -> str:
    installation_id = uuid4().hex
    term_id = uuid4().hex
    connection.execute(
        text(
            "INSERT INTO installations (id, token_hash, created_at) "
            "VALUES (:id, :hash, :created_at)"
        ),
        {"id": installation_id, "hash": uuid4().hex + uuid4().hex, "created_at": "now"},
    )
    connection.execute(
        text(
            "INSERT INTO instances (id, installation_id, name, token_hash, created_at) "
            "VALUES (:id, :installation_id, :name, :hash, :created_at)"
        ),
        {
            "id": term_id,
            "installation_id": installation_id,
            "name": "cleanup-term",
            "hash": uuid4().hex + uuid4().hex,
            "created_at": "now",
        },
    )
    connection.execute(
        text(
            "INSERT INTO agent_cleanup_jobs "
            "(id, term_id, target_kind, target_ref, state, attempt_count, "
            "created_at, updated_at) VALUES "
            "(:id, :term_id, 'term', :target_ref, :state, 0, :created_at, :updated_at)"
        ),
        {
            "id": uuid4().hex,
            "term_id": term_id,
            "target_ref": term_id,
            "state": state,
            "created_at": "now",
            "updated_at": "now",
        },
    )
    connection.commit()
    return term_id


def test_0012_upgrades_populated_0011_database(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        term_id = _seed_term_and_job(connection)

        _upgrade(connection, "head")

        columns = {
            column["name"] for column in inspect(connection).get_columns("agent_cleanup_jobs")
        }
        assert {"manifest_version", "completed_at"} <= columns
        job = connection.execute(
            text(
                "SELECT state, manifest_version, last_error "
                "FROM agent_cleanup_jobs WHERE target_ref = :target_ref"
            ),
            {"target_ref": term_id},
        ).one()
        assert job.state == "dead_letter"
        assert job.manifest_version == 1
        assert job.last_error == "legacy_cleanup_manifest_unavailable"
        receipt = connection.execute(
            text(
                "SELECT artifact_kind, artifact_ref, state, policy_reason "
                "FROM agent_cleanup_receipts WHERE artifact_ref = :target_ref"
            ),
            {"target_ref": term_id},
        ).one()
        assert receipt.artifact_kind == "legacy_manifest"
        assert receipt.state == "dead_letter"
        assert receipt.policy_reason == "legacy_cleanup_manifest_unavailable"


def test_0012_empty_database_reaches_head(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "head")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0013"
        )
        assert "agent_cleanup_receipts" in inspect(connection).get_table_names()


def test_0012_downgrades_back_to_0011(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "head")
        _downgrade(connection, "0011")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0011"
        )
        assert "agent_cleanup_receipts" not in inspect(connection).get_table_names()
        columns = {
            column["name"] for column in inspect(connection).get_columns("agent_cleanup_jobs")
        }
        assert "manifest_version" not in columns
        assert "completed_at" not in columns


def test_cleanup_receipt_unique_manifest_entry(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "head")
        term_id = _seed_term_and_job(connection)
        job_id = connection.execute(
            text("SELECT id FROM agent_cleanup_jobs WHERE target_ref = :target_ref"),
            {"target_ref": term_id},
        ).scalar_one()
        values = {
            "id": uuid4().hex,
            "job_id": job_id,
            "ref": "term-row:" + term_id,
            "created_at": "now",
            "updated_at": "now",
        }
        statement = text(
            "INSERT INTO agent_cleanup_receipts "
            "(id, cleanup_job_id, artifact_kind, artifact_ref, state, attempt_count, "
            "created_at, updated_at) VALUES "
            "(:id, :job_id, 'b_row', :ref, 'pending', 0, :created_at, :updated_at)"
        )
        connection.execute(statement, values)
        connection.commit()
        with pytest.raises(IntegrityError):
            connection.execute(statement, {**values, "id": uuid4().hex})
            connection.commit()
        connection.rollback()


def test_legacy_job_becomes_visible_dead_letter(tmp_path: Path) -> None:
    with _database(tmp_path) as connection:
        _upgrade(connection, "0011")
        term_id = _seed_term_and_job(connection, state="completed")
        _upgrade(connection, "0012")
        job = connection.execute(
            text("SELECT state, completed_at FROM agent_cleanup_jobs WHERE target_ref = :ref"),
            {"ref": term_id},
        ).one()
        assert job.state == "dead_letter"
        assert job.completed_at is None
        receipt = connection.execute(
            text(
                "SELECT state, policy_reason FROM agent_cleanup_receipts WHERE artifact_ref = :ref"
            ),
            {"ref": term_id},
        ).one()
        assert receipt.state == "dead_letter"
        assert receipt.policy_reason == "legacy_cleanup_manifest_unavailable"
