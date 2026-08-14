"""Bounded user message text storage (plan M6b spec §6.6).

Revision ID: 0010
Revises: 0009

Adds the nullable ``agent_messages.body`` column so refreshed chat views can
render historical user message text: today ``agent_messages`` carries only
``body_digest`` (M4.5 "text not persisted") and the canonical event kinds do
not include a user message, so product clients have no source for historical
user text.  The column is bounded by the ``AgentMessageRepository.create``
invariant (≤64 KiB and ``body_digest == sha256(body)`` when a body is
provided); its lifecycle is the row's lifecycle (no new purge).
Pre-migration rows keep ``body = NULL`` and clients render a degraded
placeholder (spec §6.6).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_messages",
        sa.Column("body", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_messages", "body")
