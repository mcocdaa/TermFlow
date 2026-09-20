"""Approval audit trail and display metadata (plan §12.1; task M5.2).

Revision ID: 0008
Revises: 0007

Adds the metadata-only approval audit trail and the approval display
columns the M6 approval UI needs:

* ``approval_requests.pane_id`` / ``operation`` / ``intent_summary``: bounded,
  redacted display metadata (a 1024-character intent summary; never the
  text/keys themselves).  Existing rows keep NULL and degrade gracefully.
* ``approval_audit_events``: one row per lifecycle transition
  (``created``/``decided``/``revoked``/``consumed``/``unknown``/``expired``)
  carrying agent identity (binding + instance + runtime epoch + run) and
  approval context (approval + tool call id + canonical hash + auth epoch +
  pane/operation/input byte count).  No raw text/keys columns exist;
  retention is 90 days via the startup purge sweep.

The columns and constraints exactly match ``persistence.models`` so the
packaged ``Database._validate_head_schema`` comparison passes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column("pane_id", sa.String(32), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("operation", sa.String(32), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("intent_summary", sa.Text(), nullable=True),
    )
    op.create_table(
        "approval_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=True),
        sa.Column("runtime_epoch", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("pane_id", sa.String(32), nullable=True),
        sa.Column("operation", sa.String(32), nullable=True),
        sa.Column("input_bytes", sa.Integer(), nullable=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("auth_epoch", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(256), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["approval_id"], ["approval_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_audit_events_approval_id",
        "approval_audit_events",
        ["approval_id"],
    )
    op.create_index(
        "ix_approval_audit_events_created_at",
        "approval_audit_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approval_audit_events_created_at", table_name="approval_audit_events")
    op.drop_index("ix_approval_audit_events_approval_id", table_name="approval_audit_events")
    op.drop_table("approval_audit_events")
    op.drop_column("approval_requests", "intent_summary")
    op.drop_column("approval_requests", "operation")
    op.drop_column("approval_requests", "pane_id")
