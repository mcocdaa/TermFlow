"""Per-conversation write approval policy.

Revision 0014 moves the switch to the conversation: new conversations copy
the binding default and the agent UI can flip one conversation between manual
and auto without changing its siblings.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column(
            "write_policy",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_conversations", "write_policy")
