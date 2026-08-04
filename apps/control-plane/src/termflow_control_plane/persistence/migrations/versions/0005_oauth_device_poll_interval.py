"""Persist the last OAuth device-code poll timestamp.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.add_column(
            sa.Column("device_last_polled_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.drop_column("device_last_polled_at")
