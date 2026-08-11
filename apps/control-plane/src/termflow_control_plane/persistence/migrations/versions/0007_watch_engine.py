"""Watch Engine schema delta (plan §11.2; task M3.1).

Revision ID: 0007
Revises: 0006

Adds the crash-safe watch-engine persistence the M1 schema could not carry:

* ``pane_observation_cursors``: the per-pane accepted live cursor ledger.  B
  persists the last accepted ``(pane_incarnation, stream_id, seq)`` cursor
  transactionally for every consumed live event so restart recovery resumes
  from the exact position stream continuity guarantees.
* ``watches.matcher_state``: the incremental matcher suffix/cursor for an
  ``output_contains`` literal that arrived split across chunks, so a partial
  match survives a B restart and never matches old scrollback after rearm.

The columns and constraints exactly match ``persistence.models`` so the
packaged ``Database._validate_head_schema`` comparison passes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watches",
        sa.Column("matcher_state", sa.Text(), nullable=True),
    )
    op.create_table(
        "pane_observation_cursors",
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("pane_id", sa.String(32), nullable=False),
        sa.Column("pane_incarnation", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"], ["instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("instance_id", "pane_id"),
    )


def downgrade() -> None:
    op.drop_table("pane_observation_cursors")
    op.drop_column("watches", "matcher_state")
