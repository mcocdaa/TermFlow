"""Create the pre-authentication V2 metadata schema.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_enrollment_tokens_token_hash",
        "enrollment_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("platform", sa.String(128), nullable=True),
        sa.Column("client_version", sa.String(64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_installations_token_hash", "installations", ["token_hash"], unique=True)
    op.create_table(
        "instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["installation_id"], ["installations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_instances_installation_id", "instances", ["installation_id"])
    op.create_index("ix_instances_token_hash", "instances", ["token_hash"], unique=True)
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=True),
        sa.Column("pane_id", sa.String(32), nullable=True),
        sa.Column("input_bytes", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_instance_id", "audit_events", ["instance_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("instances")
    op.drop_table("installations")
    op.drop_table("enrollment_tokens")
