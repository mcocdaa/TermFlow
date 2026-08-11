"""SQLAlchemy models that deliberately exclude terminal content."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from termflow_protocol.common import utc_now


class Base(DeclarativeBase):
    pass


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hostname: Mapped[str | None] = mapped_column(String(255), default=None)
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)
    platform: Mapped[str | None] = mapped_column(String(128), default=None)
    client_version: Mapped[str | None] = mapped_column(String(64), default=None)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("installations.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(64))
    instance_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, default=None)
    pane_id: Mapped[str | None] = mapped_column(String(32), default=None)
    input_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    result: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthenticationState(Base):
    __tablename__ = "authentication_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    totp_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    totp_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    totp_key_version: Mapped[int | None] = mapped_column(Integer, default=None)
    totp_aad_version: Mapped[int | None] = mapped_column(Integer, default=None)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    totp_last_accepted_counter: Mapped[int | None] = mapped_column(Integer, default=None)
    totp_generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TotpSetup(Base):
    __tablename__ = "totp_setups"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    setup_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    secret_key_version: Mapped[int] = mapped_column(Integer)
    secret_aad_version: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    challenge_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    context_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    context_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    context_key_version: Mapped[int] = mapped_column(Integer)
    context_aad_version: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NativeClient(Base):
    __tablename__ = "native_clients"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(128))
    public_jwk: Mapped[str] = mapped_column(Text)
    key_thumbprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(64), default=None)
    client_version: Mapped[str | None] = mapped_column(String(64), default=None)
    scopes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OAuthAuthorization(Base):
    __tablename__ = "oauth_authorizations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    transaction_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    authorization_code_digest: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )
    client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("native_clients.id"), index=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    request_state: Mapped[str] = mapped_column(String(256))
    scopes: Mapped[str] = mapped_column(Text)
    pkce_challenge: Mapped[str] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    code_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Device Authorization Grant state shares this authorization transaction.  The
    # short-lived secrets are never persisted in plaintext; only their digests are
    # stored and indexed for lookups.
    device_code_digest: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )
    user_code_digest: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )
    device_status: Mapped[str | None] = mapped_column(String(16), index=True, default=None)
    device_interval: Mapped[int | None] = mapped_column(Integer, default=None)
    device_exchanged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    device_last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def authorization_id(self) -> UUID:
        """Expose the shared authorization identifier to device-flow callers."""

        return self.id

    @property
    def status(self) -> str | None:
        """Compatibility alias for the device-flow lifecycle status."""

        return self.device_status

    @property
    def interval(self) -> int | None:
        """Compatibility alias for the server-advised polling interval."""

        return self.device_interval


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    client_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("native_clients.id"), index=True, default=None
    )
    scopes: Mapped[str] = mapped_column(Text)
    key_thumbprint: Mapped[str | None] = mapped_column(String(128), default=None)
    family_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, default=None)
    parent_token_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_tokens.id"), default=None
    )
    epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthAuditEvent(Base):
    __tablename__ = "auth_audit_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    result: Mapped[str] = mapped_column(String(32))
    source_digest: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    client_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# ---------------------------------------------------------------------------
# Agent Broker persistence models (plan §15; task M1.1 models — the Alembic
# migration is a separate task).  Invariants:
#   - No raw tokens at rest; only hashes/digests.  No provider credentials.
#   - UTC timestamps everywhere.
#   - Explicit string states, enforced by repositories/state machines later,
#     not by these columns.
#   - Explicit ON DELETE behavior so deleting a Term (`instances`) or an
#     Installation can never be blocked by orphaned Agent rows.  Cleanup jobs
#     are durable tombstones that must outlive their parent, so their links use
#     SET NULL instead of CASCADE.
# ---------------------------------------------------------------------------


class AgentProfile(Base):
    """A named backend configuration a Term can bind to."""

    __tablename__ = "agent_profiles"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    backend_kind: Mapped[str] = mapped_column(String(32))
    config: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AgentBinding(Base):
    """A profile attached to one Term (plan §3.2)."""

    __tablename__ = "agent_bindings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    term_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instances.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32))
    # Runtime fields are opaque references to the externally deployed runtime
    # and its epoch-bound MCP capability.  A binding may be created before a
    # runtime is provisioned and stays fail-closed while the runtime is not
    # ready, so they are nullable until the supervisor registers one.
    runtime_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    runtime_epoch: Mapped[int | None] = mapped_column(Integer, default=None)
    capability_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        # One active binding per (profile, Term).  Uniqueness is expressed over
        # the explicit status column rather than a partial index on active
        # rows: because status is non-null and part of the key, at most one
        # binding can occupy any given state for the same profile/Term pair,
        # so "active" is unique while terminal-state rows may coexist.
        UniqueConstraint(
            "profile_id",
            "term_id",
            "status",
            name="uq_agent_bindings_profile_term_status",
        ),
    )


class AgentMemoryScope(Base):
    """Curated B-owned memory for one binding (plan §8, §21 gate 3)."""

    __tablename__ = "agent_memory_scopes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    # Denormalized Term/Profile references so scope queries never join through
    # the binding; cascaded so Term/Profile deletion can never be blocked.
    term_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instances.id", ondelete="CASCADE"),
        index=True,
    )
    profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    byte_quota: Mapped[int] = mapped_column(Integer)
    count_quota: Mapped[int] = mapped_column(Integer)
    current_bytes: Mapped[int] = mapped_column(Integer, default=0)
    current_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AgentConversation(Base):
    """A durable product conversation owned by a binding (plan §3.2)."""

    __tablename__ = "agent_conversations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    # Title is a product-facing convenience the backend may propose later.
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class BackendConversationRef(Base):
    """Opaque backend session identity for reconciliation (plan §5)."""

    __tablename__ = "agent_backend_conversations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    backend_kind: Mapped[str] = mapped_column(String(32))
    # Backend version may be unknown until the backend reports it.
    backend_version: Mapped[str | None] = mapped_column(String(64), default=None)
    runtime_id: Mapped[str] = mapped_column(String(128))
    binding_capability_epoch: Mapped[int] = mapped_column(Integer)
    provider_ref: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentRuntimeBinding(Base):
    """Persistent runtime registration, epoch, and readiness (plan §6.2.1)."""

    __tablename__ = "agent_runtime_bindings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    runtime_ref: Mapped[str] = mapped_column(String(128))
    runtime_epoch: Mapped[int] = mapped_column(Integer)
    readiness: Mapped[str] = mapped_column(String(32))
    last_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        UniqueConstraint(
            "runtime_ref",
            "runtime_epoch",
            name="uq_agent_runtime_bindings_runtime_ref_epoch",
        ),
    )


class AgentInboxItem(Base):
    """Durable typed Agent input waiting for delivery (plan §4.2, §7)."""

    __tablename__ = "agent_inbox_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
    )
    kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_kind: Mapped[str] = mapped_column(String(32))
    auth_epoch: Mapped[int | None] = mapped_column(Integer, default=None)
    # admission_seq is assigned by the B database transaction (plan §4.2); the
    # unique (conversation_id, admission_seq) pair makes insertion/delivery
    # order deterministic and idempotent.
    admission_seq: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    payload_digest: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    delivery_state: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claim_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # causation_id is the input that caused this one; the root input in a
    # chain has none, so the column is nullable.
    causation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        # Composite "pending claims" index: the delivery worker scans by
        # delivery_state, then orders by the next retry and claim deadlines.
        Index(
            "ix_agent_inbox_items_claims",
            "delivery_state",
            "next_attempt_at",
            "claim_expires_at",
        ),
        UniqueConstraint(
            "conversation_id",
            "admission_seq",
            name="uq_agent_inbox_items_conversation_admission_seq",
        ),
    )


class AgentToolRequest(Base):
    """Side-effect request key, canonical arguments hash, and result (plan §10)."""

    __tablename__ = "agent_tool_requests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    tool_call_id: Mapped[str] = mapped_column(String(128), index=True)
    request_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    arguments_digest: Mapped[str] = mapped_column(String(64))
    recorded_result_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentRun(Base):
    """A single backend turn bound to one conversation (plan §7)."""

    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
    )
    run_state: Mapped[str] = mapped_column(String(32))
    backend_run_id: Mapped[str | None] = mapped_column(String(128), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (
        Index("ix_agent_runs_conversation_state", "conversation_id", "run_state"),
    )


class AgentMessage(Base):
    """A committed message assembly checkpoint on the product timeline (plan §7)."""

    __tablename__ = "agent_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    role: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    # Non-null assembly revision makes the unique (conversation_id,
    # assembly_revision) index "nullable-aware": it is actually enforced,
    # unlike a nullable column where SQLite would admit multiple NULLs.
    assembly_revision: Mapped[int] = mapped_column(Integer)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    body_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_agent_messages_conversation_created_at", "conversation_id", "created_at"),
        UniqueConstraint(
            "conversation_id",
            "assembly_revision",
            name="uq_agent_messages_conversation_assembly_revision",
        ),
    )


class AgentEvent(Base):
    """Canonical product event with a B-assigned database cursor (plan §4.4)."""

    __tablename__ = "agent_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    event_kind: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(128))
    # B-assigned monotonic database sequence: the unique conversation cursor.
    database_seq: Mapped[int] = mapped_column(Integer)
    payload_digest: Mapped[str] = mapped_column(String(64))
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "database_seq",
            name="uq_agent_events_conversation_database_seq",
        ),
    )


class AgentToken(Base):
    """Binding-scoped MCP capability token; only the hash is stored at rest."""

    __tablename__ = "agent_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[str] = mapped_column(Text)
    expiry_epoch: Mapped[int] = mapped_column(Integer)
    binding_epoch: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PanePolicy(Base):
    """Exact per-pane observe/write allowlist contract (plan §10)."""

    __tablename__ = "pane_policies"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
    )
    pane_id: Mapped[str] = mapped_column(String(32))
    allowed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("binding_id", "pane_id", name="uq_pane_policies_binding_pane"),
    )


class ApprovalRequest(Base):
    """Single-use assisted write approval (plan §12.1)."""

    __tablename__ = "approval_requests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    tool_call_id: Mapped[str] = mapped_column(String(128))
    canonical_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decision: Mapped[str | None] = mapped_column(String(32), default=None)
    auth_epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_approval_requests_state_expires_at", "state", "expires_at"),
        # A backend tool_call_id is opaque and only meaningful inside its
        # conversation; uniqueness is scoped to the conversation and enforced
        # because tool_call_id is non-null.
        UniqueConstraint(
            "conversation_id",
            "tool_call_id",
            name="uq_approval_requests_conversation_tool_call_id",
        ),
    )


class WriteGrant(Base):
    """Delegated write authorization (plan §12.1). Design-only; disabled by
    default in 0.2.0. The table exists so the schema is stable."""

    __tablename__ = "write_grants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    grant_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    operation_allowlist: Mapped[str] = mapped_column(Text)
    byte_quota: Mapped[int] = mapped_column(Integer)
    invocation_quota: Mapped[int] = mapped_column(Integer)
    time_window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Watch(Base):
    """A durable continuation condition on one Term pane (plan §11)."""

    __tablename__ = "watches"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    pane_id: Mapped[str] = mapped_column(String(32))
    condition_kind: Mapped[str] = mapped_column(String(32))
    start_cursor: Mapped[str] = mapped_column(Text)
    watch_generation: Mapped[int] = mapped_column(Integer)
    rearm_cursor: Mapped[str | None] = mapped_column(Text, default=None)
    intent_summary: Mapped[str] = mapped_column(Text)
    # Persisted matcher suffix/cursor for crash-safe partial matches: when an
    # ``output_contains`` literal is split across chunks, the engine stores its
    # incremental matcher state here so a restart can resume the partial match
    # (plan §11.2: "persist each active watcher's matcher suffix/cursor
    # transactionally for every consumed live event").
    matcher_state: Mapped[str | None] = mapped_column(Text, default=None)
    expiry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, default=None
    )
    one_shot: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_watches_binding_state", "binding_id", "state"),
    )


class PaneObservationCursor(Base):
    """Per-pane accepted live observation cursor (plan §11.2).

    For every consumed live pane-output event B transactionally persists the
    accepted cursor so restart recovery can resume from the exact position the
    stream continuity guarantees.  The pane-scoped key is ``(instance_id,
    pane_id)`` because tmux pane IDs can repeat across Term instances.
    """

    __tablename__ = "pane_observation_cursors"

    instance_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instances.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pane_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pane_incarnation: Mapped[str] = mapped_column(Text)
    stream_id: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WatchDelivery(Base):
    """One durable, idempotent watch trigger receipt (plan §11.2)."""

    __tablename__ = "watch_deliveries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    watch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("watches.id", ondelete="CASCADE"),
        index=True,
    )
    # Idempotency anchor: the delivery key encodes (watch, generation, cursor,
    # trigger) identity so the same trigger can never be delivered twice.
    delivery_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # Causal links.  trigger_event_id is informational only (no FK).  The
    # inbox item may be purged independently, so its link is SET NULL rather
    # than CASCADE to preserve the delivery receipt.
    trigger_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    inbox_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_inbox_items.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TranscriptDraft(Base):
    """Short-lived STT transcript awaiting user confirmation (plan §14)."""

    __tablename__ = "transcript_drafts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    binding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_bindings.id", ondelete="CASCADE"),
        index=True,
    )
    target_conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    owner_actor_id: Mapped[str] = mapped_column(String(128))
    transcript_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_transcript_drafts_state_expires_at", "state", "expires_at"),
    )


class AgentCleanupJob(Base):
    """Durable deletion tombstone that retries backend/volume cleanup (plan §17)."""

    __tablename__ = "agent_cleanup_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # These jobs are created BEFORE the parent is deleted and must outlive it,
    # so the term/installation links are SET NULL (not CASCADE) and the opaque
    # target_ref keeps the deletion target.
    term_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instances.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("installations.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("ix_agent_cleanup_jobs_state_next_attempt_at", "state", "next_attempt_at"),
    )


class AgentDiagnostic(Base):
    """Bounded, short-TTL, metadata-only diagnostics (plan §4.3, §16.1). Never
    raw provider payloads, terminal excerpts, reasoning, or credentials."""

    __tablename__ = "agent_diagnostics"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    event_kind: Mapped[str] = mapped_column(String(64))
    provider_type: Mapped[str | None] = mapped_column(String(64), default=None)
    provider_session_id: Mapped[str | None] = mapped_column(String(128), default=None)
    provider_run_id: Mapped[str | None] = mapped_column(String(128), default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    correlation_hash: Mapped[str] = mapped_column(String(64))
    payload_metadata: Mapped[str | None] = mapped_column(Text, default=None)
    ttl_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
