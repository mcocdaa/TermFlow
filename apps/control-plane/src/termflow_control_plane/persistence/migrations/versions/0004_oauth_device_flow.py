"""Persist OAuth device authorization state.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.add_column(sa.Column("device_code_digest", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("user_code_digest", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("device_status", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("device_interval", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("device_exchanged_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_oauth_authorizations_device_code_digest",
        "oauth_authorizations",
        ["device_code_digest"],
        unique=True,
    )
    op.create_index(
        "ix_oauth_authorizations_user_code_digest",
        "oauth_authorizations",
        ["user_code_digest"],
        unique=True,
    )
    op.create_index(
        "ix_oauth_authorizations_device_status",
        "oauth_authorizations",
        ["device_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oauth_authorizations_device_status",
        table_name="oauth_authorizations",
    )
    op.drop_index(
        "ix_oauth_authorizations_user_code_digest",
        table_name="oauth_authorizations",
    )
    op.drop_index(
        "ix_oauth_authorizations_device_code_digest",
        table_name="oauth_authorizations",
    )
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.drop_column("device_exchanged_at")
        batch_op.drop_column("device_interval")
        batch_op.drop_column("device_status")
        batch_op.drop_column("user_code_digest")
        batch_op.drop_column("device_code_digest")
