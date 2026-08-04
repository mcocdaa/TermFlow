"""SQLAlchemy models that deliberately exclude terminal content."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, Uuid
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
