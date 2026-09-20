"""Separate desired Agent state from observed runtime state.

Revision ID: 0011
Revises: 0010

The migration is intentionally conservative.  It rejects legacy rows that
cannot be represented without choosing or deleting data, and it never carries
the old ``ready`` meaning into the stricter 0011 readiness contract.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_BINDING_STATUSES = ("pending", "ready", "disabled", "revoked")
_NEW_BINDING_STATUSES = ("disabled", "enabled", "revoked")
_OBSERVED_READINESS_STATES = (
    "unprovisioned",
    "reconciling",
    "ready",
    "not_ready",
    "blocked",
    "disabled",
)


def _fail_if_present(statement: str, message: str) -> None:
    if op.get_bind().execute(sa.text(statement)).first() is not None:
        raise RuntimeError(message)


def _upgrade_preflight() -> None:
    legal = ", ".join(f"'{value}'" for value in _OLD_BINDING_STATUSES)
    _fail_if_present(
        f"SELECT 1 FROM agent_bindings WHERE status NOT IN ({legal}) LIMIT 1",
        "0011 upgrade found an unknown agent_bindings status; refusing to guess",
    )
    _fail_if_present(
        """
        SELECT 1
        FROM agent_bindings
        WHERE status != 'revoked'
        GROUP BY profile_id, term_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        "0011 upgrade found multiple non-revoked bindings for one profile/term; "
        "refusing to choose or discard one",
    )
    _fail_if_present(
        """
        SELECT 1
        FROM agent_runtime_bindings
        GROUP BY binding_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        "0011 upgrade found multiple agent_runtime_bindings for one binding; "
        "refusing to choose or discard one",
    )


def _downgrade_preflight() -> None:
    legal = ", ".join(f"'{value}'" for value in _NEW_BINDING_STATUSES)
    _fail_if_present(
        f"SELECT 1 FROM agent_bindings WHERE status NOT IN ({legal}) LIMIT 1",
        "0011 downgrade found an unknown agent_bindings status; refusing to guess",
    )
    _fail_if_present(
        """
        SELECT 1
        FROM agent_bindings
        WHERE status = 'revoked'
        GROUP BY profile_id, term_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        "0011 downgrade cannot represent multiple revoked history rows for one "
        "profile/term without deleting history",
    )
    _fail_if_present(
        """
        SELECT 1
        FROM agent_runtime_bindings
        WHERE observed_runtime_ref IS NULL OR observed_runtime_epoch IS NULL
        LIMIT 1
        """,
        "0011 downgrade cannot represent a runtime row without observed identity",
    )
    _fail_if_present(
        """
        SELECT 1
        FROM agent_runtime_bindings
        GROUP BY observed_runtime_ref, observed_runtime_epoch
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        "0011 downgrade found duplicate observed runtime identity; refusing to "
        "choose or discard one",
    )


def upgrade() -> None:
    _upgrade_preflight()
    _apply_upgrade(sqlite_batch=op.get_bind().dialect.name == "sqlite")


def _apply_upgrade(*, sqlite_batch: bool) -> None:
    # The preflight proves these mappings cannot collide under the old
    # (profile_id, term_id, status) uniqueness constraint.
    op.execute(
        sa.text(
            """
                UPDATE agent_bindings
                SET status = CASE status
                    WHEN 'pending' THEN 'disabled'
                    WHEN 'ready' THEN 'enabled'
                    ELSE status
                END
                """
        )
    )
    with op.batch_alter_table("agent_bindings") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default=sa.text("'disabled'"),
        )
        batch_op.add_column(
            sa.Column(
                "config_revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.drop_constraint("uq_agent_bindings_profile_term_status", type_="unique")

    op.create_index(
        "uq_agent_bindings_profile_term_active",
        "agent_bindings",
        ["profile_id", "term_id"],
        unique=True,
        sqlite_where=sa.text("status != 'revoked'"),
        postgresql_where=sa.text("status != 'revoked'"),
    )
    with op.batch_alter_table("agent_runtime_bindings") as batch_op:
        batch_op.drop_index("ix_agent_runtime_bindings_binding_id")
        batch_op.drop_constraint("uq_agent_runtime_bindings_runtime_ref_epoch", type_="unique")
        batch_op.alter_column(
            "runtime_ref",
            new_column_name="observed_runtime_ref",
            existing_type=sa.String(128),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "runtime_epoch",
            new_column_name="observed_runtime_epoch",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "readiness",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default=sa.text("'unprovisioned'"),
        )
        batch_op.add_column(sa.Column("reason_code", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("observed_capability_ref", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("applied_revision", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("config_fingerprint", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "transition_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "provider_readiness",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'configured_unverified'"),
            )
        )
        batch_op.add_column(sa.Column("provider_verified_revision", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "provider_last_checked_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("provider_reason_code", sa.String(64), nullable=True))
        batch_op.create_unique_constraint("uq_agent_runtime_bindings_binding_id", ["binding_id"])
        batch_op.create_unique_constraint(
            "uq_agent_runtime_bindings_observed_runtime_ref_epoch",
            (
                ["runtime_ref", "runtime_epoch"]
                if sqlite_batch
                else ["observed_runtime_ref", "observed_runtime_epoch"]
            ),
        )

    legal_readiness = ", ".join(f"'{value}'" for value in _OBSERVED_READINESS_STATES)
    op.execute(
        sa.text(
            f"""
                UPDATE agent_runtime_bindings
                SET readiness = 'not_ready',
                    reason_code = 'legacy_revalidation_required'
                WHERE readiness = 'ready'
                   OR readiness NOT IN ({legal_readiness})
                """
        )
    )
    op.create_index(
        "ix_agent_runtime_bindings_readiness_last_health_at",
        "agent_runtime_bindings",
        ["readiness", "last_health_at"],
    )
    op.create_index(
        "ix_agent_runtime_bindings_provider_readiness_last_checked_at",
        "agent_runtime_bindings",
        ["provider_readiness", "provider_last_checked_at"],
    )

    op.create_table(
        "agent_provider_disclosure_acceptances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("disclosure_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("endpoint_origin", sa.String(2048), nullable=False),
        sa.Column("region", sa.String(128), nullable=False),
        sa.Column("retention_terms", sa.Text(), nullable=False),
        sa.Column("retention_version", sa.String(64), nullable=False),
        sa.Column("no_training", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_auth_epoch", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(32), nullable=False),
        sa.Column("actor_ref", sa.String(256), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_provider_disclosure_acceptances_binding_disclosure_fingerprint",
        "agent_provider_disclosure_acceptances",
        ["binding_id", "disclosure_fingerprint"],
    )

    # Product setup idempotency is part of the same runtime-state boundary.
    # The receipt contains only a request digest and public outcome; no raw
    # bootstrap secret or capability token is persisted.  It is created in
    # 0011 so setup can claim the key in the same transaction as its rows.
    op.create_table(
        "agent_setup_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'activating'"),
        ),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["term_id"], ["instances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["binding_id"], ["agent_bindings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_setup_receipts_idempotency_key",
        "agent_setup_receipts",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_agent_setup_receipts_term_id",
        "agent_setup_receipts",
        ["term_id"],
    )
    op.create_index(
        "ix_agent_setup_receipts_binding_id",
        "agent_setup_receipts",
        ["binding_id"],
    )

    op.add_column(
        "auth_tokens",
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    _downgrade_preflight()
    _apply_downgrade(sqlite_batch=op.get_bind().dialect.name == "sqlite")


def _apply_downgrade(*, sqlite_batch: bool) -> None:
    op.drop_column("auth_tokens", "authenticated_at")

    op.drop_index(
        "ix_agent_provider_disclosure_acceptances_binding_disclosure_fingerprint",
        table_name="agent_provider_disclosure_acceptances",
    )
    op.drop_table("agent_provider_disclosure_acceptances")

    op.drop_index("ix_agent_setup_receipts_binding_id", table_name="agent_setup_receipts")
    op.drop_index("ix_agent_setup_receipts_term_id", table_name="agent_setup_receipts")
    op.drop_index("ix_agent_setup_receipts_idempotency_key", table_name="agent_setup_receipts")
    op.drop_table("agent_setup_receipts")

    op.drop_index(
        "ix_agent_runtime_bindings_provider_readiness_last_checked_at",
        table_name="agent_runtime_bindings",
    )
    op.drop_index(
        "ix_agent_runtime_bindings_readiness_last_health_at",
        table_name="agent_runtime_bindings",
    )
    with op.batch_alter_table("agent_runtime_bindings") as batch_op:
        batch_op.drop_constraint(
            "uq_agent_runtime_bindings_observed_runtime_ref_epoch",
            type_="unique",
        )
        batch_op.drop_constraint("uq_agent_runtime_bindings_binding_id", type_="unique")
        batch_op.alter_column(
            "observed_runtime_ref",
            new_column_name="runtime_ref",
            existing_type=sa.String(128),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "observed_runtime_epoch",
            new_column_name="runtime_epoch",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "readiness",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.drop_column("reason_code")
        batch_op.drop_column("observed_capability_ref")
        batch_op.drop_column("applied_revision")
        batch_op.drop_column("config_fingerprint")
        batch_op.drop_column("transition_started_at")
        batch_op.drop_column("provider_readiness")
        batch_op.drop_column("provider_verified_revision")
        batch_op.drop_column("provider_last_checked_at")
        batch_op.drop_column("provider_reason_code")
        batch_op.create_unique_constraint(
            "uq_agent_runtime_bindings_runtime_ref_epoch",
            (
                ["observed_runtime_ref", "observed_runtime_epoch"]
                if sqlite_batch
                else ["runtime_ref", "runtime_epoch"]
            ),
        )
        batch_op.create_index("ix_agent_runtime_bindings_binding_id", ["binding_id"])

    op.execute(
        sa.text(
            """
                UPDATE agent_bindings
                SET status = CASE status
                    WHEN 'enabled' THEN 'ready'
                    ELSE status
                END
                """
        )
    )
    op.drop_index(
        "uq_agent_bindings_profile_term_active",
        table_name="agent_bindings",
    )
    with op.batch_alter_table("agent_bindings") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.drop_column("config_revision")
        batch_op.create_unique_constraint(
            "uq_agent_bindings_profile_term_status",
            ["profile_id", "term_id", "status"],
        )
