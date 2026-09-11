"""Binding write approval policy (manual vs auto).

Revision 0013 adds the two-mode policy the agent panel exposes: ``manual``
keeps the single-use human approval flow, ``auto`` lets the binding
pre-approve every allowlisted write.  Existing bindings stay fail-closed on
the manual default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot ALTER-ADD a check constraint; the two allowed values are
    # enforced by the admin API and the column default keeps old rows manual.
    op.add_column(
        "agent_bindings",
        sa.Column(
            "write_policy",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_bindings", "write_policy")
