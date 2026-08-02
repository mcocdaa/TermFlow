"""Add persistent unified authentication state.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authentication_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("totp_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("totp_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("totp_key_version", sa.Integer(), nullable=True),
        sa.Column("totp_aad_version", sa.Integer(), nullable=True),
        sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totp_last_accepted_counter", sa.Integer(), nullable=True),
        sa.Column("totp_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_authentication_state_singleton"),
        sa.CheckConstraint("epoch >= 1", name="ck_authentication_state_epoch"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO authentication_state (id, epoch, updated_at) "
            "VALUES (1, 1, CURRENT_TIMESTAMP)"
        )
    )
    op.create_table(
        "totp_setups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("setup_digest", sa.String(64), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("secret_key_version", sa.Integer(), nullable=False),
        sa.Column("secret_aad_version", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_totp_setups_setup_digest", "totp_setups", ["setup_digest"], unique=True)
    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("challenge_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("context_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("context_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("context_key_version", sa.Integer(), nullable=False),
        sa.Column("context_aad_version", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_challenges_challenge_digest", "auth_challenges", ["challenge_digest"], unique=True
    )
    op.create_index("ix_auth_challenges_kind", "auth_challenges", ["kind"])
    op.create_table(
        "native_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("public_jwk", sa.Text(), nullable=False),
        sa.Column("key_thumbprint", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(64), nullable=True),
        sa.Column("client_version", sa.String(64), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_native_clients_key_thumbprint",
        "native_clients",
        ["key_thumbprint"],
        unique=True,
    )
    op.create_table(
        "oauth_authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_digest", sa.String(64), nullable=False),
        sa.Column("authorization_code_digest", sa.String(64), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("request_state", sa.String(256), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("pkce_challenge", sa.String(128), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["native_clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_authorizations_client_id", "oauth_authorizations", ["client_id"])
    op.create_index(
        "ix_oauth_authorizations_transaction_digest",
        "oauth_authorizations",
        ["transaction_digest"],
        unique=True,
    )
    op.create_index(
        "ix_oauth_authorizations_authorization_code_digest",
        "oauth_authorizations",
        ["authorization_code_digest"],
        unique=True,
    )
    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("key_thumbprint", sa.String(128), nullable=True),
        sa.Column("family_id", sa.Uuid(), nullable=True),
        sa.Column("parent_token_id", sa.Uuid(), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["native_clients.id"]),
        sa.ForeignKeyConstraint(["parent_token_id"], ["auth_tokens.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_tokens_token_digest", "auth_tokens", ["token_digest"], unique=True)
    op.create_index("ix_auth_tokens_kind", "auth_tokens", ["kind"])
    op.create_index("ix_auth_tokens_client_id", "auth_tokens", ["client_id"])
    op.create_index("ix_auth_tokens_family_id", "auth_tokens", ["family_id"])
    op.create_index("ix_auth_tokens_expires_at", "auth_tokens", ["expires_at"])
    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_audit_events_operation", "auth_audit_events", ["operation"])
    op.create_index("ix_auth_audit_events_source_digest", "auth_audit_events", ["source_digest"])
    op.create_index("ix_auth_audit_events_client_id", "auth_audit_events", ["client_id"])


def downgrade() -> None:
    op.drop_table("auth_audit_events")
    op.drop_table("auth_tokens")
    op.drop_table("oauth_authorizations")
    op.drop_table("native_clients")
    op.drop_table("auth_challenges")
    op.drop_table("totp_setups")
    op.drop_table("authentication_state")
