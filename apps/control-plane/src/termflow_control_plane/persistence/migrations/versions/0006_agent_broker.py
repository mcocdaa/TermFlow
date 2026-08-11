"""Create the Agent Broker persistence schema (plan §15; task M1.1).

Revision ID: 0006
Revises: 0005

Creates every Agent Broker table exactly as declared in
``termflow_control_plane.persistence.models``: columns, nullability, foreign
keys with explicit ``ON DELETE`` behavior, unique constraints, and indexes
(including the composite pending-claims index and retry deadlines).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("backend_kind", sa.String(32), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_profiles_display_name",
        "agent_profiles",
        ["display_name"],
        unique=True,
    )
    op.create_table(
        "agent_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("runtime_ref", sa.String(128), nullable=True),
        sa.Column("runtime_epoch", sa.Integer(), nullable=True),
        sa.Column("capability_ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["agent_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["instances.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "term_id",
            "status",
            name="uq_agent_bindings_profile_term_status",
        ),
    )
    op.create_index("ix_agent_bindings_profile_id", "agent_bindings", ["profile_id"])
    op.create_index("ix_agent_bindings_term_id", "agent_bindings", ["term_id"])
    op.create_table(
        "agent_memory_scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("byte_quota", sa.Integer(), nullable=False),
        sa.Column("count_quota", sa.Integer(), nullable=False),
        sa.Column("current_bytes", sa.Integer(), nullable=False),
        sa.Column("current_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["instances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["agent_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_memory_scopes_binding_id",
        "agent_memory_scopes",
        ["binding_id"],
        unique=True,
    )
    op.create_index("ix_agent_memory_scopes_term_id", "agent_memory_scopes", ["term_id"])
    op.create_index(
        "ix_agent_memory_scopes_profile_id",
        "agent_memory_scopes",
        ["profile_id"],
    )
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_conversations_binding_id",
        "agent_conversations",
        ["binding_id"],
    )
    op.create_table(
        "agent_backend_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("backend_kind", sa.String(32), nullable=False),
        sa.Column("backend_version", sa.String(64), nullable=True),
        sa.Column("runtime_id", sa.String(128), nullable=False),
        sa.Column("binding_capability_epoch", sa.Integer(), nullable=False),
        sa.Column("provider_ref", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_backend_conversations_conversation_id",
        "agent_backend_conversations",
        ["conversation_id"],
    )
    op.create_index(
        "ix_agent_backend_conversations_provider_ref",
        "agent_backend_conversations",
        ["provider_ref"],
        unique=True,
    )
    op.create_table(
        "agent_runtime_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_ref", sa.String(128), nullable=False),
        sa.Column("runtime_epoch", sa.Integer(), nullable=False),
        sa.Column("readiness", sa.String(32), nullable=False),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_ref",
            "runtime_epoch",
            name="uq_agent_runtime_bindings_runtime_ref_epoch",
        ),
    )
    op.create_index(
        "ix_agent_runtime_bindings_binding_id",
        "agent_runtime_bindings",
        ["binding_id"],
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_state", sa.String(32), nullable=False),
        sa.Column("backend_run_id", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_runs_conversation_state",
        "agent_runs",
        ["conversation_id", "run_state"],
    )
    op.create_table(
        "agent_inbox_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_kind", sa.String(32), nullable=False),
        sa.Column("auth_epoch", sa.Integer(), nullable=True),
        sa.Column("admission_seq", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("delivery_state", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("claim_owner", sa.String(128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "admission_seq",
            name="uq_agent_inbox_items_conversation_admission_seq",
        ),
    )
    op.create_index(
        "ix_agent_inbox_items_idempotency_key",
        "agent_inbox_items",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_agent_inbox_items_claims",
        "agent_inbox_items",
        ["delivery_state", "next_attempt_at", "claim_expires_at"],
    )
    op.create_table(
        "agent_tool_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("recorded_result_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_tool_requests_binding_id",
        "agent_tool_requests",
        ["binding_id"],
    )
    op.create_index("ix_agent_tool_requests_run_id", "agent_tool_requests", ["run_id"])
    op.create_index(
        "ix_agent_tool_requests_tool_call_id",
        "agent_tool_requests",
        ["tool_call_id"],
    )
    op.create_index(
        "ix_agent_tool_requests_request_key",
        "agent_tool_requests",
        ["request_key"],
        unique=True,
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("assembly_revision", sa.Integer(), nullable=False),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column("body_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "assembly_revision",
            name="uq_agent_messages_conversation_assembly_revision",
        ),
    )
    op.create_index("ix_agent_messages_run_id", "agent_messages", ["run_id"])
    op.create_index(
        "ix_agent_messages_conversation_created_at",
        "agent_messages",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("database_seq", sa.Integer(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "database_seq",
            name="uq_agent_events_conversation_database_seq",
        ),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("expiry_epoch", sa.Integer(), nullable=False),
        sa.Column("binding_epoch", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_tokens_binding_id", "agent_tokens", ["binding_id"])
    op.create_index(
        "ix_agent_tokens_token_hash",
        "agent_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "pane_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("pane_id", sa.String(32), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("binding_id", "pane_id", name="uq_pane_policies_binding_pane"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(32), nullable=True),
        sa.Column("auth_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "tool_call_id",
            name="uq_approval_requests_conversation_tool_call_id",
        ),
    )
    op.create_index(
        "ix_approval_requests_binding_id",
        "approval_requests",
        ["binding_id"],
    )
    op.create_index("ix_approval_requests_run_id", "approval_requests", ["run_id"])
    op.create_index(
        "ix_approval_requests_state_expires_at",
        "approval_requests",
        ["state", "expires_at"],
    )
    op.create_table(
        "write_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.String(128), nullable=False),
        sa.Column("operation_allowlist", sa.Text(), nullable=False),
        sa.Column("byte_quota", sa.Integer(), nullable=False),
        sa.Column("invocation_quota", sa.Integer(), nullable=False),
        sa.Column("time_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_write_grants_binding_id", "write_grants", ["binding_id"])
    op.create_index("ix_write_grants_grant_id", "write_grants", ["grant_id"], unique=True)
    op.create_table(
        "watches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("pane_id", sa.String(32), nullable=False),
        sa.Column("condition_kind", sa.String(32), nullable=False),
        sa.Column("start_cursor", sa.Text(), nullable=False),
        sa.Column("watch_generation", sa.Integer(), nullable=False),
        sa.Column("rearm_cursor", sa.Text(), nullable=True),
        sa.Column("intent_summary", sa.Text(), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("one_shot", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watches_conversation_id", "watches", ["conversation_id"])
    op.create_index("ix_watches_expiry_at", "watches", ["expiry_at"])
    op.create_index("ix_watches_binding_state", "watches", ["binding_id", "state"])
    op.create_table(
        "watch_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("watch_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_key", sa.String(128), nullable=False),
        sa.Column("trigger_event_id", sa.Uuid(), nullable=True),
        sa.Column("inbox_item_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["watch_id"], ["watches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["inbox_item_id"], ["agent_inbox_items.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watch_deliveries_watch_id", "watch_deliveries", ["watch_id"])
    op.create_index(
        "ix_watch_deliveries_delivery_key",
        "watch_deliveries",
        ["delivery_key"],
        unique=True,
    )
    op.create_index(
        "ix_watch_deliveries_inbox_item_id",
        "watch_deliveries",
        ["inbox_item_id"],
    )
    op.create_index(
        "ix_watch_deliveries_next_attempt_at",
        "watch_deliveries",
        ["next_attempt_at"],
    )
    op.create_table(
        "transcript_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("target_conversation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_actor_id", sa.String(128), nullable=False),
        sa.Column("transcript_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transcript_drafts_binding_id",
        "transcript_drafts",
        ["binding_id"],
    )
    op.create_index(
        "ix_transcript_drafts_target_conversation_id",
        "transcript_drafts",
        ["target_conversation_id"],
    )
    op.create_index(
        "ix_transcript_drafts_state_expires_at",
        "transcript_drafts",
        ["state", "expires_at"],
    )
    op.create_table(
        "agent_cleanup_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=True),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target_ref", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["term_id"], ["instances.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["installations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_cleanup_jobs_term_id", "agent_cleanup_jobs", ["term_id"])
    op.create_index(
        "ix_agent_cleanup_jobs_installation_id",
        "agent_cleanup_jobs",
        ["installation_id"],
    )
    op.create_index(
        "ix_agent_cleanup_jobs_state_next_attempt_at",
        "agent_cleanup_jobs",
        ["state", "next_attempt_at"],
    )
    op.create_table(
        "agent_diagnostics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("provider_type", sa.String(64), nullable=True),
        sa.Column("provider_session_id", sa.String(128), nullable=True),
        sa.Column("provider_run_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("correlation_hash", sa.String(64), nullable=False),
        sa.Column("payload_metadata", sa.Text(), nullable=True),
        sa.Column("ttl_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_diagnostics_conversation_id",
        "agent_diagnostics",
        ["conversation_id"],
    )
    op.create_index(
        "ix_agent_diagnostics_ttl_expires_at",
        "agent_diagnostics",
        ["ttl_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("agent_diagnostics")
    op.drop_table("agent_cleanup_jobs")
    op.drop_table("transcript_drafts")
    op.drop_table("watch_deliveries")
    op.drop_table("watches")
    op.drop_table("write_grants")
    op.drop_table("approval_requests")
    op.drop_table("pane_policies")
    op.drop_table("agent_tokens")
    op.drop_table("agent_events")
    op.drop_table("agent_messages")
    op.drop_table("agent_tool_requests")
    op.drop_table("agent_inbox_items")
    op.drop_table("agent_runs")
    op.drop_table("agent_runtime_bindings")
    op.drop_table("agent_backend_conversations")
    op.drop_table("agent_conversations")
    op.drop_table("agent_memory_scopes")
    op.drop_table("agent_bindings")
    op.drop_table("agent_profiles")
