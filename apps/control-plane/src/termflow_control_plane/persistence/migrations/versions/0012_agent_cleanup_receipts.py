"""Versioned cleanup manifests and per-artifact receipts.

Revision 0012 makes deletion state auditable.  Rows written by the pre-0012
tombstone implementation had no manifest, so they are deliberately surfaced
as dead-letter work instead of being treated as an empty successful job.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_REASON = "legacy_cleanup_manifest_unavailable"


def _deduplicate_legacy_jobs() -> None:
    """Collapse duplicate pre-0012 tombstones before adding the target key.

    The old schema had no target uniqueness.  Keeping the oldest row gives
    retries one stable identity; duplicate rows carried no child receipts and
    therefore contain no evidence that would be lost by this normalization.
    """

    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, target_kind, target_ref FROM agent_cleanup_jobs ORDER BY created_at, id"
            )
        )
        .mappings()
        .all()
    )
    seen: set[tuple[str, str]] = set()
    duplicate_ids: list[object] = []
    for row in rows:
        key = (str(row["target_kind"]), str(row["target_ref"]))
        if key in seen:
            duplicate_ids.append(row["id"])
        else:
            seen.add(key)
    for job_id in duplicate_ids:
        bind.execute(
            sa.text("DELETE FROM agent_cleanup_jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )


def _mark_legacy_jobs() -> None:
    """Attach one dead-letter receipt to every legacy job."""

    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, target_kind, target_ref, state "
                "FROM agent_cleanup_jobs ORDER BY created_at, id"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO agent_cleanup_receipts "
                "(id, cleanup_job_id, artifact_kind, artifact_ref, state, "
                "attempt_count, last_error, next_attempt_at, evidence_digest, "
                "policy_reason, policy_version, confirmed_at, created_at, updated_at) "
                "VALUES (:id, :job_id, 'legacy_manifest', :artifact_ref, "
                "'dead_letter', 0, :reason, NULL, NULL, :reason, '0012', "
                "NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "id": uuid4().hex,
                "job_id": row["id"],
                "artifact_ref": str(row["target_ref"]),
                "reason": _LEGACY_REASON,
            },
        )
        bind.execute(
            sa.text(
                "UPDATE agent_cleanup_jobs SET state = 'dead_letter', "
                "last_error = :reason, next_attempt_at = NULL, "
                "completed_at = NULL WHERE id = :job_id"
            ),
            {"reason": _LEGACY_REASON, "job_id": row["id"]},
        )


def upgrade() -> None:
    # Keep the server default: newly-created jobs always carry an explicit
    # manifest version even when inserted by a SQL client.
    op.add_column(
        "agent_cleanup_jobs",
        sa.Column("manifest_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "agent_cleanup_jobs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "agent_cleanup_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cleanup_job_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_kind", sa.String(64), nullable=False),
        sa.Column("artifact_ref", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.Column("confirmation_key", sa.Uuid(), nullable=True),
        sa.Column("policy_reason", sa.String(128), nullable=True),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cleanup_job_id"], ["agent_cleanup_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cleanup_job_id",
            "artifact_kind",
            "artifact_ref",
            name="uq_agent_cleanup_receipts_manifest_entry",
        ),
    )
    op.create_index(
        "ix_agent_cleanup_receipts_cleanup_job_id",
        "agent_cleanup_receipts",
        ["cleanup_job_id"],
    )
    op.create_index(
        "ix_agent_cleanup_receipts_state_next_attempt_at",
        "agent_cleanup_receipts",
        ["state", "next_attempt_at"],
    )

    # Normalize legacy rows before enforcing one manifest per target.
    _deduplicate_legacy_jobs()
    _mark_legacy_jobs()
    op.create_index(
        "uq_agent_cleanup_jobs_target",
        "agent_cleanup_jobs",
        ["target_kind", "target_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_agent_cleanup_jobs_target", table_name="agent_cleanup_jobs")
    op.drop_index(
        "ix_agent_cleanup_receipts_state_next_attempt_at",
        table_name="agent_cleanup_receipts",
    )
    op.drop_index(
        "ix_agent_cleanup_receipts_cleanup_job_id",
        table_name="agent_cleanup_receipts",
    )
    op.drop_table("agent_cleanup_receipts")
    op.drop_column("agent_cleanup_jobs", "completed_at")
    op.drop_column("agent_cleanup_jobs", "manifest_version")
