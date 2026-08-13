"""Bounded canonical event payload storage (plan M6a spec §4.2).

Revision ID: 0009
Revises: 0007

Adds the nullable ``agent_events.payload`` column that the AG-UI wire
projection needs: today ``agent_events`` carries only ``payload_digest`` (no
content), so live and replay delivery have nothing to project.  The column is
bounded by the ``AgentEventRepository.append`` invariant (≤64 KiB and
``payload_digest == sha256(payload_json)`` when a payload is provided); its
lifecycle is the row's lifecycle (no new purge).  Pre-migration rows keep
``payload = NULL`` and are explicitly dropped by the AG-UI projection with a
diagnostic counter (spec §4.3/§4.6).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_events",
        sa.Column("payload", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_events", "payload")
