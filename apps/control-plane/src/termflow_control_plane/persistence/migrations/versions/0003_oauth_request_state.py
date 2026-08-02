"""Persist native OAuth callback state.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.add_column(sa.Column("request_state", sa.String(256), nullable=True))
        batch_op.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
    # Revision 0002 could not preserve the caller's state. Refuse to resume those
    # in-flight grants after upgrade instead of returning an invented callback state.
    op.execute(
        sa.text(
            "UPDATE oauth_authorizations "
            "SET request_state = '', consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP)"
        )
    )
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.alter_column("request_state", existing_type=sa.String(256), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("oauth_authorizations") as batch_op:
        batch_op.drop_column("attempts")
        batch_op.drop_column("request_state")
