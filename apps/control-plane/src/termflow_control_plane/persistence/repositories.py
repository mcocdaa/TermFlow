"""Small transactional repositories for Control Plane metadata."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.agent_contracts import RETENTION_MATRIX, DataClass
from termflow_control_plane.auth.context import as_utc
from termflow_control_plane.auth.pkce import create_s256_challenge
from termflow_control_plane.auth.secret_box import EncryptedSecret

from .models import (
    MAX_AGENT_EVENT_PAYLOAD_BYTES,
    MAX_AGENT_MESSAGE_BODY_BYTES,
    AgentBinding,
    AgentCleanupJob,
    AgentCleanupReceipt,
    AgentConversation,
    AgentDiagnostic,
    AgentEvent,
    AgentInboxItem,
    AgentMemoryScope,
    AgentMessage,
    AgentProfile,
    AgentProviderDisclosureAcceptance,
    AgentRun,
    AgentRuntimeBinding,
    AgentSetupReceipt,
    AgentToken,
    AgentToolRequest,
    ApprovalAuditEvent,
    ApprovalRequest,
    AuditEvent,
    AuthAuditEvent,
    AuthChallenge,
    AuthenticationState,
    AuthToken,
    BackendConversationRef,
    EnrollmentToken,
    Installation,
    Instance,
    NativeClient,
    OAuthAuthorization,
    PanePolicy,
    TotpSetup,
    Watch,
    WatchDelivery,
)


def digest_secret(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


#: Retention window for Agent inbox/run rows in terminal recovery states and
#: for confirmed cleanup tombstones (plan §16.1: the durable timeline and
#: confirmed tombstones are retained 30 days).
AGENT_TERMINAL_RETENTION = timedelta(days=30)

#: Retention window for approval audit metadata (spec §7 and the
#: ``APPROVAL_AUDIT_METADATA`` contract: 90 days, redacted, no raw storage).
AGENT_APPROVAL_AUDIT_RETENTION = timedelta(days=90)

#: §16.1 retention ceilings wired from the executable contract module
#: (``termflow_control_plane.agent_contracts.RETENTION_MATRIX``): streaming
#: assembly checkpoints (ephemeral ``MESSAGE_DELTA`` events and non-final
#: message checkpoints) live 24 hours; final product messages and the durable
#: timeline live 30 days.  The contract allows a data class whose ceiling is
#: governed elsewhere (``retention is None``); for these two classes the
#: defaults below fall back to the §16.1 table values if the matrix is ever
#: changed to drop the explicit ceiling.


def _matrix_retention(data_class: DataClass, default: timedelta) -> timedelta:
    """A §16.1 ceiling from RETENTION_MATRIX, or ``default`` when governed
    elsewhere (``retention is None``).  An explicit zero retention is honored
    (immediate expiry), never silently replaced by the default."""
    policy = RETENTION_MATRIX[data_class].retention
    return default if policy is None else policy


AGENT_ASSEMBLY_CHECKPOINT_RETENTION = _matrix_retention(
    DataClass.ASSEMBLY_CHECKPOINTS, timedelta(hours=24)
)
AGENT_FINAL_TIMELINE_RETENTION = _matrix_retention(DataClass.FINAL_MESSAGES, timedelta(days=30))


def _encode_scopes(scopes: tuple[str, ...]) -> str:
    return json.dumps(sorted(set(scopes)), separators=(",", ":"))


def decode_scopes(encoded: str) -> tuple[str, ...]:
    value = json.loads(encoded)
    if not isinstance(value, list) or not all(isinstance(scope, str) for scope in value):
        raise ValueError("persisted authentication scopes are malformed")
    return tuple(value)


async def _insert_auth_token(
    session: AsyncSession,
    *,
    raw_token: str,
    kind: str,
    encoded_scopes: str,
    key_thumbprint: str | None,
    expires_at: datetime,
    epoch: int,
    client_id: UUID | None = None,
    family_id: UUID | None = None,
    parent_token_id: UUID | None = None,
    authenticated_at: datetime | None = None,
    now: datetime | None = None,
) -> AuthToken | None:
    observed_at = as_utc(now or datetime.now(UTC))
    persisted_authenticated_at = as_utc(authenticated_at) if authenticated_at is not None else None
    token_id = uuid4()
    effective_family_id = family_id or (uuid4() if kind == "refresh" else None)
    conditions = [
        exists(
            select(AuthenticationState.id).where(
                AuthenticationState.id == 1,
                AuthenticationState.epoch == epoch,
            )
        )
    ]
    if client_id is not None:
        conditions.append(
            exists(
                select(NativeClient.id).where(
                    NativeClient.id == client_id,
                    NativeClient.key_thumbprint == key_thumbprint,
                    NativeClient.revoked_at.is_(None),
                )
            )
        )
    source = select(
        literal(token_id),
        literal(digest_secret(raw_token)),
        literal(kind),
        literal(client_id),
        literal(encoded_scopes),
        literal(key_thumbprint),
        literal(effective_family_id),
        literal(parent_token_id),
        literal(epoch),
        literal(expires_at),
        literal(persisted_authenticated_at),
        literal(observed_at),
    ).where(*conditions)
    result = await session.execute(
        insert(AuthToken)
        .from_select(
            [
                AuthToken.id,
                AuthToken.token_digest,
                AuthToken.kind,
                AuthToken.client_id,
                AuthToken.scopes,
                AuthToken.key_thumbprint,
                AuthToken.family_id,
                AuthToken.parent_token_id,
                AuthToken.epoch,
                AuthToken.expires_at,
                AuthToken.authenticated_at,
                AuthToken.created_at,
            ],
            source,
        )
        .returning(AuthToken)
    )
    token = result.scalar_one_or_none()
    if token is not None:
        # SQLite's DateTime type returns naive values even when timezone=True.
        # Normalize the detached ORM view at this boundary so callers cannot
        # accidentally compare a persisted UTC instant as local time.
        if token.authenticated_at is not None:
            token.authenticated_at = as_utc(token.authenticated_at)
        await session.execute(
            update(NativeClient)
            .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
            .values(last_used_at=observed_at, updated_at=observed_at)
        )
    return token


class InstanceOwnershipError(RuntimeError):
    pass


class InstallationRevoked(RuntimeError):
    pass


class NativeClientRevoked(RuntimeError):
    pass


class AuthenticationStateChanged(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConsumedEnrollment:
    id: UUID
    display_name: str | None


@dataclass(frozen=True, slots=True)
class ExchangedAuthorization:
    authorization: OAuthAuthorization
    access_token: AuthToken
    refresh_token: AuthToken


@dataclass(frozen=True, slots=True)
class RotatedTokenPair:
    access_token: AuthToken
    refresh_token: AuthToken


@dataclass(frozen=True, slots=True)
class CreatedDeviceAuthorization:
    """Raw device-flow values returned once to the requesting native client."""

    id: UUID
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    expires_at: datetime
    interval: int
    status: str = "pending"

    @property
    def authorization_id(self) -> UUID:
        return self.id


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationRequest:
    """Persistence-facing input for creating a device authorization transaction."""

    client_id: UUID
    scopes: tuple[str, ...]
    pkce_challenge: str
    expires_at: datetime
    epoch: int = 1
    interval: int = 5
    redirect_uri: str = ""
    request_state: str = ""
    transaction_secret: str | None = None


class EnrollmentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        token_hash: str,
        expires_at: datetime,
        *,
        display_name: str | None = None,
    ) -> EnrollmentToken:
        async with self._sessions() as session:
            enrollment = EnrollmentToken(
                token_hash=token_hash,
                display_name=display_name,
                expires_at=expires_at,
            )
            session.add(enrollment)
            await session.commit()
            return enrollment

    async def consume(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> ConsumedEnrollment | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(EnrollmentToken)
                .where(
                    EnrollmentToken.token_hash == token_hash,
                    EnrollmentToken.used_at.is_(None),
                    EnrollmentToken.expires_at > observed_at,
                )
                .values(used_at=observed_at)
                .returning(EnrollmentToken.id, EnrollmentToken.display_name)
            )
            consumed = result.one_or_none()
            await session.commit()
            if consumed is None:
                return None
            return ConsumedEnrollment(id=consumed[0], display_name=consumed[1])


class InstallationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        token_hash: str,
        *,
        hostname: str | None = None,
        display_name: str | None = None,
        platform: str | None = None,
        client_version: str | None = None,
    ) -> Installation:
        async with self._sessions() as session:
            installation = Installation(
                token_hash=token_hash,
                hostname=hostname,
                display_name=display_name or hostname or "Computer",
                platform=platform,
                client_version=client_version,
            )
            session.add(installation)
            await session.commit()
            return installation

    async def get(self, installation_id: UUID) -> Installation | None:
        async with self._sessions() as session:
            return await session.get(Installation, installation_id)

    async def get_by_token_hash(self, token_hash: str) -> Installation | None:
        async with self._sessions() as session:
            installation: Installation | None = await session.scalar(
                select(Installation).where(
                    Installation.token_hash == token_hash,
                    Installation.revoked_at.is_(None),
                )
            )
            return installation

    async def list_all(self) -> list[Installation]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Installation)
                .where(Installation.revoked_at.is_(None))
                .order_by(Installation.created_at)
            )
            return list(result)

    async def rename(self, installation_id: UUID, display_name: str) -> Installation | None:
        async with self._sessions() as session:
            installation = await session.get(Installation, installation_id)
            if installation is None:
                return None
            installation.display_name = display_name
            await session.commit()
            return installation

    async def delete(self, installation_id: UUID, *, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            installation = await session.scalar(
                select(Installation).where(
                    Installation.id == installation_id,
                    Installation.revoked_at.is_(None),
                )
            )
            if installation is None:
                return False
            installation.revoked_at = observed_at
            await session.execute(
                delete(Instance).where(Instance.installation_id == installation_id)
            )
            await session.commit()
            return True


class InstanceRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def register_or_rotate(
        self,
        instance_id: UUID,
        installation_id: UUID,
        name: str,
        token_hash: str,
    ) -> Instance:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            installation = await session.get(Installation, installation_id)
            if installation is None or installation.revoked_at is not None:
                raise InstallationRevoked(str(installation_id))
            instance = await session.get(Instance, instance_id)
            if instance is None:
                instance = Instance(
                    id=instance_id,
                    installation_id=installation_id,
                    name=name,
                    token_hash=token_hash,
                    last_seen_at=observed_at,
                )
                session.add(instance)
            elif instance.installation_id != installation_id:
                raise InstanceOwnershipError(str(instance_id))
            else:
                instance.name = name
                instance.token_hash = token_hash
                instance.revoked_at = None
                instance.last_seen_at = observed_at
            await session.execute(
                update(Installation)
                .where(Installation.id == installation_id)
                .values(last_seen_at=observed_at)
            )
            await session.commit()
            return instance

    async def touch(self, instance_id: UUID, *, now: datetime | None = None) -> None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            instance = await session.get(Instance, instance_id)
            if instance is None:
                return
            instance.last_seen_at = observed_at
            await session.execute(
                update(Installation)
                .where(Installation.id == instance.installation_id)
                .values(last_seen_at=observed_at)
            )
            await session.commit()

    async def get(self, instance_id: UUID) -> Instance | None:
        async with self._sessions() as session:
            return await session.get(Instance, instance_id)

    async def delete(self, instance_id: UUID) -> bool:
        async with self._sessions() as session:
            instance = await session.get(Instance, instance_id)
            if instance is None:
                return False
            await session.delete(instance)
            await session.commit()
            return True

    async def get_by_token_hash(self, token_hash: str) -> Instance | None:
        async with self._sessions() as session:
            instance: Instance | None = await session.scalar(
                select(Instance).where(
                    Instance.token_hash == token_hash,
                    Instance.revoked_at.is_(None),
                )
            )
            return instance

    async def list_all(self) -> list[Instance]:
        async with self._sessions() as session:
            result = await session.scalars(select(Instance).order_by(Instance.created_at))
            return list(result)

    async def list_for_installation(self, installation_id: UUID) -> list[Instance]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Instance)
                .where(
                    Instance.installation_id == installation_id,
                    Instance.revoked_at.is_(None),
                )
                .order_by(Instance.created_at)
            )
            return list(result)

    async def rename(self, instance_id: UUID, name: str) -> Instance | None:
        async with self._sessions() as session:
            instance = await session.get(Instance, instance_id)
            if instance is None:
                return None
            instance.name = name
            instance.last_seen_at = datetime.now(UTC)
            await session.commit()
            return instance

    async def update_from_topology(
        self,
        instance_id: UUID,
        session_name: str,
        *,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            instance = await session.get(Instance, instance_id)
            if instance is None:
                return
            instance.name = session_name
            instance.last_seen_at = observed_at
            await session.execute(
                update(Installation)
                .where(Installation.id == instance.installation_id)
                .values(last_seen_at=observed_at)
            )
            await session.commit()


class AuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> AuditEvent:
        async with self._sessions() as session:
            event = AuditEvent(
                operation=operation,
                instance_id=instance_id,
                pane_id=pane_id,
                input_bytes=input_bytes,
                result=result,
                error_code=error_code,
            )
            session.add(event)
            await session.commit()
            return event

    async def list_all(self) -> list[AuditEvent]:
        async with self._sessions() as session:
            result = await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
            return list(result)

    async def count_since(self, since: datetime) -> int:
        async with self._sessions() as session:
            count = await session.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.created_at >= since)
            )
            return int(count or 0)


class AuthStateRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self) -> AuthenticationState:
        async with self._sessions() as session:
            state = await session.get(AuthenticationState, 1)
            if state is None:
                raise RuntimeError("authentication state singleton is missing")
            return state

    async def configure_totp(
        self,
        encrypted: EncryptedSecret,
        counter: int,
        *,
        expected_epoch: int,
        expected_generation: int,
        enabled: bool,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(
                    AuthenticationState.id == 1,
                    AuthenticationState.epoch == expected_epoch,
                    AuthenticationState.totp_generation == expected_generation,
                )
                .values(
                    totp_ciphertext=encrypted.ciphertext,
                    totp_nonce=encrypted.nonce,
                    totp_key_version=encrypted.key_version,
                    totp_aad_version=encrypted.aad_version,
                    totp_enabled_at=observed_at if enabled else None,
                    totp_last_accepted_counter=counter,
                    totp_generation=AuthenticationState.totp_generation + 1,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.id)
            )
            updated = result.scalar_one_or_none() is not None
            await session.commit()
            return updated

    async def enable_totp_protection(
        self,
        counter: int,
        *,
        expected_epoch: int,
        expected_generation: int,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(
                    AuthenticationState.id == 1,
                    AuthenticationState.epoch == expected_epoch,
                    AuthenticationState.totp_generation == expected_generation,
                    AuthenticationState.totp_ciphertext.is_not(None),
                    AuthenticationState.totp_nonce.is_not(None),
                    AuthenticationState.totp_key_version.is_not(None),
                    AuthenticationState.totp_aad_version.is_not(None),
                    AuthenticationState.totp_enabled_at.is_(None),
                    or_(
                        AuthenticationState.totp_last_accepted_counter.is_(None),
                        AuthenticationState.totp_last_accepted_counter < counter,
                    ),
                )
                .values(
                    totp_enabled_at=observed_at,
                    totp_last_accepted_counter=counter,
                    totp_generation=AuthenticationState.totp_generation + 1,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.id)
            )
            enabled_now = result.scalar_one_or_none() is not None
            await session.commit()
            return enabled_now

    async def disable_totp_protection(
        self,
        *,
        expected_epoch: int,
        expected_generation: int,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(
                    AuthenticationState.id == 1,
                    AuthenticationState.epoch == expected_epoch,
                    AuthenticationState.totp_generation == expected_generation,
                    AuthenticationState.totp_enabled_at.is_not(None),
                )
                .values(
                    totp_enabled_at=None,
                    totp_generation=AuthenticationState.totp_generation + 1,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.id)
            )
            disabled = result.scalar_one_or_none() is not None
            await session.commit()
            return disabled

    async def accept_totp_counter(
        self,
        counter: int,
        *,
        epoch: int,
        generation: int,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(
                    AuthenticationState.id == 1,
                    AuthenticationState.epoch == epoch,
                    AuthenticationState.totp_generation == generation,
                    AuthenticationState.totp_ciphertext.is_not(None),
                    AuthenticationState.totp_nonce.is_not(None),
                    AuthenticationState.totp_key_version.is_not(None),
                    AuthenticationState.totp_aad_version.is_not(None),
                    or_(
                        AuthenticationState.totp_last_accepted_counter.is_(None),
                        AuthenticationState.totp_last_accepted_counter < counter,
                    ),
                )
                .values(totp_last_accepted_counter=counter, updated_at=observed_at)
                .returning(AuthenticationState.id)
            )
            accepted = result.scalar_one_or_none() is not None
            await session.commit()
            return accepted

    async def rotate_credentials(
        self,
        *,
        audit_source_digest: str | None = None,
    ) -> int:
        """Revoke every Web, native, and CLI credential without clearing TOTP.

        Bumps the persisted epoch exactly like `reset_and_increment_epoch` but
        leaves the TOTP seed intact so rotation does not force re-enrollment.
        """

        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(AuthenticationState.id == 1)
                .values(
                    epoch=AuthenticationState.epoch + 1,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.epoch)
            )
            epoch = result.scalar_one_or_none()
            if epoch is None:
                raise RuntimeError("authentication state singleton is missing")
            await session.execute(
                update(AuthChallenge)
                .where(AuthChallenge.completed_at.is_(None))
                .values(completed_at=observed_at)
            )
            await session.execute(
                update(AuthToken)
                .where(AuthToken.revoked_at.is_(None))
                .values(revoked_at=observed_at)
            )
            await session.execute(
                update(OAuthAuthorization)
                .where(OAuthAuthorization.consumed_at.is_(None))
                .values(consumed_at=observed_at)
            )
            session.add(
                AuthAuditEvent(
                    operation="auth.rotate",
                    result="rotated",
                    source_digest=audit_source_digest or digest_secret("local-control-plane"),
                    created_at=observed_at,
                )
            )
            await session.commit()
            return int(epoch)

    async def reset_and_increment_epoch(
        self,
        *,
        audit_source_digest: str | None = None,
    ) -> int:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(AuthenticationState.id == 1)
                .values(
                    epoch=AuthenticationState.epoch + 1,
                    totp_ciphertext=None,
                    totp_nonce=None,
                    totp_key_version=None,
                    totp_aad_version=None,
                    totp_enabled_at=None,
                    totp_last_accepted_counter=None,
                    totp_generation=AuthenticationState.totp_generation + 1,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.epoch)
            )
            epoch = result.scalar_one_or_none()
            if epoch is None:
                raise RuntimeError("authentication state singleton is missing")
            await session.execute(delete(TotpSetup))
            await session.execute(
                update(AuthChallenge)
                .where(AuthChallenge.completed_at.is_(None))
                .values(completed_at=observed_at)
            )
            await session.execute(
                update(AuthToken)
                .where(AuthToken.revoked_at.is_(None))
                .values(revoked_at=observed_at)
            )
            await session.execute(
                update(OAuthAuthorization)
                .where(OAuthAuthorization.consumed_at.is_(None))
                .values(consumed_at=observed_at)
            )
            session.add(
                AuthAuditEvent(
                    operation="auth.reset",
                    result="reset",
                    source_digest=audit_source_digest or digest_secret("local-control-plane"),
                    created_at=observed_at,
                )
            )
            await session.commit()
            return int(epoch)


class TotpSetupRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        encrypted: EncryptedSecret,
        *,
        expires_at: datetime,
        epoch: int,
    ) -> UUID:
        setup_id = uuid4()
        async with self._sessions() as session:
            session.add(
                TotpSetup(
                    setup_digest=digest_secret(str(setup_id)),
                    secret_ciphertext=encrypted.ciphertext,
                    secret_nonce=encrypted.nonce,
                    secret_key_version=encrypted.key_version,
                    secret_aad_version=encrypted.aad_version,
                    expires_at=expires_at,
                    epoch=epoch,
                )
            )
            await session.commit()
        return setup_id

    async def get_active(
        self,
        setup_id: UUID,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> EncryptedSecret | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(
                        TotpSetup.secret_ciphertext,
                        TotpSetup.secret_nonce,
                        TotpSetup.secret_key_version,
                        TotpSetup.secret_aad_version,
                    ).where(
                        TotpSetup.setup_digest == digest_secret(str(setup_id)),
                        TotpSetup.epoch == epoch,
                        TotpSetup.expires_at > observed_at,
                        TotpSetup.consumed_at.is_(None),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return EncryptedSecret(row[0], row[1], row[2], row[3])

    async def consume(
        self,
        setup_id: UUID,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> EncryptedSecret | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(TotpSetup)
                .where(
                    TotpSetup.setup_digest == digest_secret(str(setup_id)),
                    TotpSetup.epoch == epoch,
                    TotpSetup.expires_at > observed_at,
                    TotpSetup.consumed_at.is_(None),
                )
                .values(consumed_at=observed_at)
                .returning(
                    TotpSetup.secret_ciphertext,
                    TotpSetup.secret_nonce,
                    TotpSetup.secret_key_version,
                    TotpSetup.secret_aad_version,
                )
            )
            row = result.one_or_none()
            await session.commit()
            if row is None:
                return None
            return EncryptedSecret(row[0], row[1], row[2], row[3])


class AuthChallengeRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        kind: str,
        encrypted_context: EncryptedSecret,
        *,
        expires_at: datetime,
        epoch: int,
    ) -> UUID:
        challenge_id = uuid4()
        async with self._sessions() as session:
            session.add(
                AuthChallenge(
                    challenge_digest=digest_secret(str(challenge_id)),
                    kind=kind,
                    context_ciphertext=encrypted_context.ciphertext,
                    context_nonce=encrypted_context.nonce,
                    context_key_version=encrypted_context.key_version,
                    context_aad_version=encrypted_context.aad_version,
                    expires_at=expires_at,
                    epoch=epoch,
                )
            )
            await session.commit()
        return challenge_id

    async def get_active(
        self,
        challenge_id: UUID,
        kind: str,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> EncryptedSecret | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(
                        AuthChallenge.context_ciphertext,
                        AuthChallenge.context_nonce,
                        AuthChallenge.context_key_version,
                        AuthChallenge.context_aad_version,
                    ).where(
                        AuthChallenge.challenge_digest == digest_secret(str(challenge_id)),
                        AuthChallenge.kind == kind,
                        AuthChallenge.epoch == epoch,
                        AuthChallenge.expires_at > observed_at,
                        AuthChallenge.completed_at.is_(None),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return EncryptedSecret(row[0], row[1], row[2], row[3])

    async def fail_attempt(
        self,
        challenge_id: UUID,
        maximum: int = 5,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthChallenge)
                .where(
                    AuthChallenge.challenge_digest == digest_secret(str(challenge_id)),
                    AuthChallenge.completed_at.is_(None),
                    AuthChallenge.expires_at > observed_at,
                    AuthChallenge.attempts < maximum,
                )
                .values(
                    attempts=AuthChallenge.attempts + 1,
                    completed_at=case(
                        (AuthChallenge.attempts + 1 >= maximum, observed_at),
                        else_=AuthChallenge.completed_at,
                    ),
                )
                .returning(AuthChallenge.id)
            )
            updated = result.scalar_one_or_none() is not None
            await session.commit()
            return updated

    async def consume(
        self,
        challenge_id: UUID,
        kind: str,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> EncryptedSecret | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthChallenge)
                .where(
                    AuthChallenge.challenge_digest == digest_secret(str(challenge_id)),
                    AuthChallenge.kind == kind,
                    AuthChallenge.epoch == epoch,
                    AuthChallenge.expires_at > observed_at,
                    AuthChallenge.completed_at.is_(None),
                )
                .values(completed_at=observed_at)
                .returning(
                    AuthChallenge.context_ciphertext,
                    AuthChallenge.context_nonce,
                    AuthChallenge.context_key_version,
                    AuthChallenge.context_aad_version,
                )
            )
            row = result.one_or_none()
            await session.commit()
            if row is None:
                return None
            return EncryptedSecret(row[0], row[1], row[2], row[3])


class NativeClientRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        display_name: str,
        public_jwk: str,
        key_thumbprint: str,
        platform: str | None,
        scopes: tuple[str, ...],
        client_id: UUID | None = None,
        client_version: str | None = None,
    ) -> NativeClient:
        async with self._sessions() as session:
            client = NativeClient(
                id=client_id or uuid4(),
                display_name=display_name,
                public_jwk=public_jwk,
                key_thumbprint=key_thumbprint,
                platform=platform,
                client_version=client_version,
                scopes=_encode_scopes(scopes),
            )
            session.add(client)
            await session.commit()
            return client

    async def get_or_create(
        self,
        *,
        display_name: str,
        public_jwk: str,
        key_thumbprint: str,
        platform: str | None,
        scopes: tuple[str, ...],
        client_version: str | None = None,
    ) -> NativeClient:
        """Return the stable JKT owner, including across concurrent first authorization.

        A previously revoked client is reactivated: revocation ends existing
        sessions, but the same device key may start a fresh authorization
        flow (kick off, not permanent ban).
        """

        existing = await self.get_by_thumbprint(key_thumbprint)
        if existing is not None:
            if existing.revoked_at is not None:
                return await self.reactivate(existing.id) or existing
            return existing
        try:
            return await self.create(
                display_name=display_name,
                public_jwk=public_jwk,
                key_thumbprint=key_thumbprint,
                platform=platform,
                client_version=client_version,
                scopes=scopes,
            )
        except IntegrityError:
            winner = await self.get_by_thumbprint(key_thumbprint)
            if winner is None:
                raise
            if winner.revoked_at is not None:
                return await self.reactivate(winner.id) or winner
            return winner

    async def get(self, client_id: UUID) -> NativeClient | None:
        async with self._sessions() as session:
            return await session.get(NativeClient, client_id)

    async def get_by_thumbprint(self, key_thumbprint: str) -> NativeClient | None:
        async with self._sessions() as session:
            client: NativeClient | None = await session.scalar(
                select(NativeClient).where(NativeClient.key_thumbprint == key_thumbprint)
            )
            return client

    async def get_active_by_thumbprint(self, key_thumbprint: str) -> NativeClient | None:
        async with self._sessions() as session:
            client: NativeClient | None = await session.scalar(
                select(NativeClient).where(
                    NativeClient.key_thumbprint == key_thumbprint,
                    NativeClient.revoked_at.is_(None),
                )
            )
            return client

    async def list_all(self) -> list[NativeClient]:
        async with self._sessions() as session:
            rows = await session.scalars(select(NativeClient).order_by(NativeClient.created_at))
            return list(rows)

    async def list_authorized(self) -> list[NativeClient]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(NativeClient)
                .where(
                    NativeClient.revoked_at.is_(None),
                    exists(
                        select(OAuthAuthorization.id).where(
                            OAuthAuthorization.client_id == NativeClient.id,
                            OAuthAuthorization.approved_at.is_not(None),
                        )
                    ),
                )
                .order_by(NativeClient.created_at)
            )
            return list(rows)

    async def touch(
        self,
        client_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(NativeClient)
                .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
                .values(last_used_at=observed_at, updated_at=observed_at)
                .returning(NativeClient.id)
            )
            touched = result.scalar_one_or_none() is not None
            await session.commit()
            return touched

    async def update_scopes(self, client_id: UUID, scopes: tuple[str, ...]) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                update(NativeClient)
                .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
                .values(scopes=_encode_scopes(scopes), updated_at=datetime.now(UTC))
                .returning(NativeClient.id)
            )
            updated = result.scalar_one_or_none() is not None
            await session.commit()
            return updated

    async def rename(self, client_id: UUID, display_name: str) -> NativeClient | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(NativeClient)
                .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
                .values(display_name=display_name, updated_at=datetime.now(UTC))
                .returning(NativeClient)
            )
            client = result.scalar_one_or_none()
            await session.commit()
            return client

    async def revoke(self, client_id: UUID) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(NativeClient)
                .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
                .values(revoked_at=observed_at, updated_at=observed_at)
                .returning(NativeClient.id)
            )
            revoked = result.scalar_one_or_none() is not None
            if revoked:
                await session.execute(
                    update(AuthToken)
                    .where(AuthToken.client_id == client_id, AuthToken.revoked_at.is_(None))
                    .values(revoked_at=observed_at)
                )
            await session.commit()
            return revoked

    async def reactivate(self, client_id: UUID) -> NativeClient | None:
        """Clear revocation so the same device key can start a new flow.

        Old tokens stay revoked; the next successful authorization issues
        fresh ones.
        """

        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(NativeClient)
                .where(NativeClient.id == client_id, NativeClient.revoked_at.is_not(None))
                .values(revoked_at=None, updated_at=observed_at)
                .returning(NativeClient)
            )
            client = result.scalar_one_or_none()
            await session.commit()
            return client


class OAuthAuthorizationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _request_value(request: object | None, name: str, default: object = None) -> object:
        if request is None:
            return default
        if isinstance(request, Mapping):
            return request.get(name, default)
        return getattr(request, name, default)

    @staticmethod
    def _new_user_code() -> str:
        # Avoid visually ambiguous characters while retaining enough entropy for
        # the short-lived code shown in the Web C approval UI.
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))

    async def create(
        self,
        *,
        transaction_secret: str,
        client_id: UUID,
        redirect_uri: str,
        scopes: tuple[str, ...],
        pkce_challenge: str,
        expires_at: datetime,
        epoch: int,
        request_state: str,
    ) -> UUID:
        async with self._sessions() as session:
            authorization = OAuthAuthorization(
                transaction_digest=digest_secret(transaction_secret),
                client_id=client_id,
                redirect_uri=redirect_uri,
                request_state=request_state,
                scopes=_encode_scopes(scopes),
                pkce_challenge=pkce_challenge,
                expires_at=expires_at,
                epoch=epoch,
            )
            session.add(authorization)
            await session.commit()
            return authorization.id

    async def create_device_authorization(
        self,
        request: DeviceAuthorizationRequest | Mapping[str, object] | object | None = None,
        *,
        client_id: UUID | None = None,
        scopes: tuple[str, ...] | None = None,
        pkce_challenge: str | None = None,
        expires_at: datetime | None = None,
        epoch: int | None = None,
        interval: int | None = None,
        redirect_uri: str | None = None,
        request_state: str | None = None,
        transaction_secret: str | None = None,
        device_code: str | None = None,
        user_code: str | None = None,
        now: datetime | None = None,
        ttl_seconds: int = 900,
    ) -> CreatedDeviceAuthorization:
        """Create a device transaction and return the raw codes exactly once.

        The method accepts a :class:`DeviceAuthorizationRequest` (or a compatible
        object/mapping) as a convenience for service callers, while explicit
        keyword arguments remain available to keep this repository independent of
        HTTP DTOs.  Only digests of the two returned codes are stored.
        """

        request_client_id = cast(UUID | None, self._request_value(request, "client_id"))
        request_scopes = cast(
            tuple[str, ...] | None,
            self._request_value(request, "scopes"),
        )
        request_pkce = cast(str | None, self._request_value(request, "pkce_challenge"))
        request_expires = cast(datetime | None, self._request_value(request, "expires_at"))
        request_epoch = cast(int | None, self._request_value(request, "epoch"))
        request_interval = cast(int | None, self._request_value(request, "interval"))
        request_redirect = cast(str | None, self._request_value(request, "redirect_uri"))
        request_state_value = cast(
            str | None,
            self._request_value(request, "request_state"),
        )
        request_transaction = cast(
            str | None,
            self._request_value(request, "transaction_secret"),
        )

        effective_client_id = client_id if client_id is not None else request_client_id
        effective_scopes = scopes if scopes is not None else request_scopes
        effective_pkce = pkce_challenge if pkce_challenge is not None else request_pkce
        effective_epoch = epoch if epoch is not None else request_epoch
        effective_interval = interval if interval is not None else request_interval
        effective_redirect = redirect_uri if redirect_uri is not None else request_redirect
        effective_state = request_state if request_state is not None else request_state_value
        effective_transaction = (
            transaction_secret if transaction_secret is not None else request_transaction
        )
        if effective_client_id is None or not isinstance(effective_client_id, UUID):
            raise ValueError("device authorization requires a native client id")
        if effective_scopes is None:
            raise ValueError("device authorization requires scopes")
        if effective_pkce is None:
            raise ValueError("device authorization requires a PKCE challenge")

        observed_at = now or datetime.now(UTC)
        effective_expires = expires_at if expires_at is not None else request_expires
        if effective_expires is None:
            if ttl_seconds <= 0:
                raise ValueError("device authorization TTL must be positive")
            effective_expires = observed_at + timedelta(seconds=ttl_seconds)
        effective_epoch = 1 if effective_epoch is None else int(effective_epoch)
        effective_interval = 5 if effective_interval is None else int(effective_interval)
        if effective_interval <= 0:
            raise ValueError("device authorization polling interval must be positive")
        effective_redirect = "" if effective_redirect is None else str(effective_redirect)
        effective_state = "" if effective_state is None else str(effective_state)
        effective_transaction = (
            secrets.token_urlsafe(32)
            if effective_transaction is None
            else str(effective_transaction)
        )
        raw_device_code = device_code or secrets.token_urlsafe(32)
        raw_user_code = user_code or self._new_user_code()
        authorization_id = uuid4()
        async with self._sessions() as session:
            authorization = OAuthAuthorization(
                id=authorization_id,
                transaction_digest=digest_secret(effective_transaction),
                client_id=effective_client_id,
                redirect_uri=effective_redirect,
                request_state=effective_state,
                scopes=_encode_scopes(tuple(str(scope) for scope in effective_scopes)),
                pkce_challenge=str(effective_pkce),
                expires_at=effective_expires,
                epoch=effective_epoch,
                device_code_digest=digest_secret(raw_device_code),
                user_code_digest=digest_secret(raw_user_code),
                device_status="pending",
                device_interval=effective_interval,
                created_at=observed_at,
            )
            session.add(authorization)
            await session.commit()
            return CreatedDeviceAuthorization(
                id=authorization.id,
                device_code=raw_device_code,
                user_code=raw_user_code,
                expires_at=effective_expires,
                interval=effective_interval,
            )

    async def find_by_device_code(
        self,
        raw_device_code: str,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Find a pending/approved transaction without returning the raw code."""

        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            authorization: OAuthAuthorization | None = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status.in_(("pending", "approved")),
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
            )
            return authorization

    async def record_device_poll(
        self,
        raw_device_code: str,
        *,
        epoch: int,
        interval: int,
        now: datetime | None = None,
    ) -> int | None:
        """Atomically record a poll, returning seconds to wait when too early."""

        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - timedelta(seconds=interval)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status.in_(("pending", "approved")),
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    or_(
                        OAuthAuthorization.device_last_polled_at.is_(None),
                        OAuthAuthorization.device_last_polled_at <= cutoff,
                    ),
                )
                .values(device_last_polled_at=observed_at)
                .returning(OAuthAuthorization.device_last_polled_at)
            )
            recorded = result.scalar_one_or_none()
            if recorded is not None:
                await session.commit()
                return None
            last_polled = await session.scalar(
                select(OAuthAuthorization.device_last_polled_at).where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                )
            )
            await session.commit()
            if last_polled is None:
                return None
            if last_polled.tzinfo is None:
                last_polled = last_polled.replace(tzinfo=UTC)
            wait_seconds = (last_polled + timedelta(seconds=interval) - observed_at).total_seconds()
            return max(1, math.ceil(wait_seconds))

    async def get_device_authorization(
        self,
        raw_device_code: str,
        *,
        epoch: int = 1,
    ) -> OAuthAuthorization | None:
        """Read terminal device state without exposing the raw code."""

        async with self._sessions() as session:
            authorization: OAuthAuthorization | None = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                )
            )
            return authorization

    async def find_by_user_code(
        self,
        raw_user_code: str,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Find a pending/approved transaction by the short user code."""

        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            authorization: OAuthAuthorization | None = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.user_code_digest == digest_secret(raw_user_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status.in_(("pending", "approved")),
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
            )
            return authorization

    async def mark_approved(
        self,
        authorization_id: UUID,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Atomically move a live pending device request to approved."""

        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == authorization_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status == "pending",
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                )
                .values(
                    device_status="approved",
                    approved_at=func.coalesce(OAuthAuthorization.approved_at, observed_at),
                )
                .returning(OAuthAuthorization)
            )
            approved = result.scalar_one_or_none()
            await session.commit()
            return approved

    async def mark_denied(
        self,
        authorization_id: UUID,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Atomically move a live pending device request to denied."""

        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == authorization_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status == "pending",
                    OAuthAuthorization.device_exchanged_at.is_(None),
                )
                .values(device_status="denied", consumed_at=observed_at)
                .returning(OAuthAuthorization)
            )
            denied = result.scalar_one_or_none()
            await session.commit()
            return denied

    async def deny_device_authorization(
        self,
        authorization_id: UUID,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Named alias used by service layers for the device denial transition."""

        return await self.mark_denied(authorization_id, epoch=epoch, now=now)

    async def exchange_device_code(
        self,
        raw_device_code: str,
        code_verifier: str,
        *,
        epoch: int = 1,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        """Claim an approved device transaction exactly once.

        The conditional update is the one-time exchange boundary: concurrent
        callers race on the same row, and only the caller that observes the
        unexchanged marker can return the authorization.
        """

        observed_at = now or datetime.now(UTC)
        try:
            challenge = create_s256_challenge(code_verifier)
        except (TypeError, ValueError):
            return None
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.pkce_challenge == challenge,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status == "approved",
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
                .values(
                    device_status="exchanged",
                    device_exchanged_at=observed_at,
                    consumed_at=observed_at,
                )
                .returning(OAuthAuthorization)
            )
            exchanged = result.scalar_one_or_none()
            await session.commit()
            return exchanged

    async def exchange_device_code_with_tokens(
        self,
        raw_device_code: str,
        code_verifier: str,
        *,
        epoch: int,
        raw_access_token: str,
        raw_refresh_token: str,
        key_thumbprint: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
        now: datetime | None = None,
    ) -> ExchangedAuthorization | None:
        """Atomically claim a device code and issue its native token pair."""

        observed_at = now or datetime.now(UTC)
        try:
            challenge = create_s256_challenge(code_verifier)
        except (TypeError, ValueError):
            return None
        active_client = exists(
            select(NativeClient.id).where(
                NativeClient.id == OAuthAuthorization.client_id,
                NativeClient.key_thumbprint == key_thumbprint,
                NativeClient.revoked_at.is_(None),
            )
        )
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.device_code_digest == digest_secret(raw_device_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.pkce_challenge == challenge,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.device_status == "approved",
                    OAuthAuthorization.device_exchanged_at.is_(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    active_client,
                )
                .values(
                    device_status="exchanged",
                    device_exchanged_at=observed_at,
                    consumed_at=observed_at,
                )
                .returning(OAuthAuthorization)
            )
            authorization = result.scalar_one_or_none()
            if authorization is None:
                await session.commit()
                return None
            family_id = uuid4()
            access = await _insert_auth_token(
                session,
                raw_token=raw_access_token,
                kind="access",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=access_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            refresh = await _insert_auth_token(
                session,
                raw_token=raw_refresh_token,
                kind="refresh",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=refresh_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            if access is None or refresh is None:
                await session.rollback()
                return None
            await session.commit()
            return ExchangedAuthorization(authorization, access, refresh)

    async def get_active_id(
        self,
        transaction_id: UUID,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            authorization: OAuthAuthorization | None = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.id == transaction_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
            )
            return authorization

    async def deny(
        self,
        transaction_id: UUID,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == transaction_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                    OAuthAuthorization.authorization_code_digest.is_(None),
                )
                .values(consumed_at=observed_at)
                .returning(OAuthAuthorization)
            )
            denied = result.scalar_one_or_none()
            await session.commit()
            return denied

    async def record_failure(
        self,
        transaction_id: UUID,
        *,
        epoch: int,
        maximum: int,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        next_attempt = OAuthAuthorization.attempts + 1
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == transaction_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                )
                .values(
                    attempts=next_attempt,
                    consumed_at=case(
                        (next_attempt >= maximum, observed_at),
                        else_=OAuthAuthorization.consumed_at,
                    ),
                )
                .returning(OAuthAuthorization.attempts)
            )
            recorded = result.scalar_one_or_none() is not None
            await session.commit()
            return recorded

    async def get_active_transaction(
        self,
        transaction_secret: str,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            authorization: OAuthAuthorization | None = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.transaction_digest == digest_secret(transaction_secret),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
            )
            return authorization

    async def issue_code(
        self,
        authorization_id: UUID,
        raw_code: str,
        *,
        epoch: int = 1,
        code_ttl_seconds: int = 60,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == authorization_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.authorization_code_digest.is_(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    OAuthAuthorization.expires_at > observed_at,
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    exists(
                        select(NativeClient.id).where(
                            NativeClient.id == OAuthAuthorization.client_id,
                            NativeClient.revoked_at.is_(None),
                        )
                    ),
                )
                .values(
                    authorization_code_digest=digest_secret(raw_code),
                    code_issued_at=observed_at,
                    code_expires_at=observed_at + timedelta(seconds=code_ttl_seconds),
                    approved_at=func.coalesce(OAuthAuthorization.approved_at, observed_at),
                )
                .returning(OAuthAuthorization.id)
            )
            issued = result.scalar_one_or_none() is not None
            await session.commit()
            return issued

    async def exchange_code(
        self,
        raw_code: str,
        *,
        epoch: int,
        raw_access_token: str,
        raw_refresh_token: str,
        key_thumbprint: str,
        pkce_challenge: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
        now: datetime | None = None,
    ) -> ExchangedAuthorization | None:
        observed_at = now or datetime.now(UTC)
        active_client = exists(
            select(NativeClient.id).where(
                NativeClient.id == OAuthAuthorization.client_id,
                NativeClient.key_thumbprint == key_thumbprint,
                NativeClient.revoked_at.is_(None),
            )
        )
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.authorization_code_digest == digest_secret(raw_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.pkce_challenge == pkce_challenge,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.code_expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    active_client,
                )
                .values(consumed_at=observed_at)
                .returning(OAuthAuthorization)
            )
            authorization = result.scalar_one_or_none()
            if authorization is None:
                await session.commit()
                return None
            family_id = uuid4()
            access = await _insert_auth_token(
                session,
                raw_token=raw_access_token,
                kind="access",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=access_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            refresh = await _insert_auth_token(
                session,
                raw_token=raw_refresh_token,
                kind="refresh",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=refresh_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            if access is None or refresh is None:
                await session.rollback()
                return None
            await session.commit()
            return ExchangedAuthorization(
                authorization=authorization,
                access_token=access,
                refresh_token=refresh,
            )

    async def exchange_transaction(
        self,
        transaction_id: UUID,
        *,
        epoch: int,
        raw_access_token: str,
        raw_refresh_token: str,
        key_thumbprint: str,
        pkce_challenge: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
        now: datetime | None = None,
    ) -> ExchangedAuthorization | None:
        observed_at = now or datetime.now(UTC)
        active_client = exists(
            select(NativeClient.id).where(
                NativeClient.id == OAuthAuthorization.client_id,
                NativeClient.key_thumbprint == key_thumbprint,
                NativeClient.revoked_at.is_(None),
            )
        )
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == transaction_id,
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.pkce_challenge == pkce_challenge,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.code_expires_at > observed_at,
                    OAuthAuthorization.authorization_code_digest.is_not(None),
                    OAuthAuthorization.approved_at.is_not(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    exists(
                        select(AuthenticationState.id).where(
                            AuthenticationState.id == 1,
                            AuthenticationState.epoch == epoch,
                        )
                    ),
                    active_client,
                )
                .values(consumed_at=observed_at)
                .returning(OAuthAuthorization)
            )
            authorization = result.scalar_one_or_none()
            if authorization is None:
                await session.commit()
                return None
            family_id = uuid4()
            access = await _insert_auth_token(
                session,
                raw_token=raw_access_token,
                kind="access",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=access_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            refresh = await _insert_auth_token(
                session,
                raw_token=raw_refresh_token,
                kind="refresh",
                encoded_scopes=authorization.scopes,
                key_thumbprint=key_thumbprint,
                expires_at=refresh_expires_at,
                epoch=epoch,
                client_id=authorization.client_id,
                family_id=family_id,
                authenticated_at=authorization.approved_at,
                now=observed_at,
            )
            if access is None or refresh is None:
                await session.rollback()
                return None
            await session.commit()
            return ExchangedAuthorization(authorization, access, refresh)


class AuthTokenRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def issue(
        self,
        raw_token: str,
        *,
        kind: str,
        scopes: tuple[str, ...],
        key_thumbprint: str | None,
        expires_at: datetime,
        epoch: int,
        client_id: UUID | None = None,
        family_id: UUID | None = None,
        parent_token_id: UUID | None = None,
        authenticated_at: datetime | None = None,
    ) -> AuthToken:
        if kind not in {"access", "refresh", "cli"}:
            raise ValueError("unsupported authentication token kind")
        async with self._sessions() as session:
            token = await _insert_auth_token(
                session,
                raw_token=raw_token,
                kind=kind,
                encoded_scopes=_encode_scopes(scopes),
                key_thumbprint=key_thumbprint,
                expires_at=expires_at,
                epoch=epoch,
                client_id=client_id,
                family_id=family_id,
                parent_token_id=parent_token_id,
                authenticated_at=authenticated_at,
            )
            if token is None:
                state = await session.get(AuthenticationState, 1)
                epoch_changed = state is None or state.epoch != epoch
                await session.rollback()
                if epoch_changed:
                    raise AuthenticationStateChanged("authentication epoch changed")
                raise NativeClientRevoked("native client is missing, revoked, or key-mismatched")
            await session.commit()
            return token

    async def get_active(
        self,
        raw_token: str,
        *,
        epoch: int,
        kind: str | None = None,
        now: datetime | None = None,
    ) -> AuthToken | None:
        observed_at = now or datetime.now(UTC)
        conditions = [
            AuthToken.token_digest == digest_secret(raw_token),
            AuthToken.epoch == epoch,
            AuthToken.expires_at > observed_at,
            AuthToken.revoked_at.is_(None),
            AuthToken.rotated_at.is_(None),
            exists(
                select(AuthenticationState.id).where(
                    AuthenticationState.id == 1,
                    AuthenticationState.epoch == epoch,
                )
            ),
        ]
        if kind is not None:
            conditions.append(AuthToken.kind == kind)
        conditions.append(
            or_(
                AuthToken.client_id.is_(None),
                exists(
                    select(NativeClient.id).where(
                        NativeClient.id == AuthToken.client_id,
                        NativeClient.key_thumbprint == AuthToken.key_thumbprint,
                        NativeClient.revoked_at.is_(None),
                    )
                ),
            )
        )
        async with self._sessions() as session:
            token: AuthToken | None = await session.scalar(select(AuthToken).where(*conditions))
            if token is not None and token.authenticated_at is not None:
                token.authenticated_at = as_utc(token.authenticated_at)
            return token

    async def rotate_refresh(
        self,
        raw_refresh: str,
        replacement: str,
        *,
        expires_at: datetime,
        epoch: int,
        now: datetime | None = None,
    ) -> AuthToken | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token_digest == digest_secret(raw_refresh),
                    AuthToken.kind == "refresh",
                    AuthToken.epoch == epoch,
                    AuthToken.expires_at > observed_at,
                    AuthToken.rotated_at.is_(None),
                    AuthToken.revoked_at.is_(None),
                )
                .values(rotated_at=observed_at)
                .returning(
                    AuthToken.id,
                    AuthToken.client_id,
                    AuthToken.scopes,
                    AuthToken.key_thumbprint,
                    AuthToken.family_id,
                    AuthToken.authenticated_at,
                )
            )
            row = result.one_or_none()
            if row is None:
                replay_family = await session.scalar(
                    select(AuthToken.family_id).where(
                        AuthToken.token_digest == digest_secret(raw_refresh),
                        AuthToken.kind == "refresh",
                        AuthToken.epoch == epoch,
                        AuthToken.rotated_at.is_not(None),
                    )
                )
                if replay_family is not None:
                    await session.execute(
                        update(AuthToken)
                        .where(
                            AuthToken.family_id == replay_family,
                            AuthToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=observed_at)
                    )
                await session.commit()
                return None
            token = await _insert_auth_token(
                session,
                raw_token=replacement,
                kind="refresh",
                client_id=row[1],
                encoded_scopes=row[2],
                key_thumbprint=row[3],
                family_id=row[4],
                parent_token_id=row[0],
                epoch=epoch,
                expires_at=expires_at,
                authenticated_at=row[5],
                now=observed_at,
            )
            if token is None:
                state = await session.get(AuthenticationState, 1)
                epoch_changed = state is None or state.epoch != epoch
                await session.rollback()
                if epoch_changed:
                    raise AuthenticationStateChanged("authentication epoch changed")
                raise NativeClientRevoked("native client was revoked during refresh rotation")
            await session.commit()
            return token

    async def rotate_refresh_pair(
        self,
        raw_refresh: str,
        replacement_refresh: str,
        raw_access: str,
        *,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
        epoch: int,
        key_thumbprint: str,
        now: datetime | None = None,
    ) -> RotatedTokenPair | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token_digest == digest_secret(raw_refresh),
                    AuthToken.kind == "refresh",
                    AuthToken.epoch == epoch,
                    AuthToken.key_thumbprint == key_thumbprint,
                    AuthToken.expires_at > observed_at,
                    AuthToken.rotated_at.is_(None),
                    AuthToken.revoked_at.is_(None),
                )
                .values(rotated_at=observed_at)
                .returning(
                    AuthToken.id,
                    AuthToken.client_id,
                    AuthToken.scopes,
                    AuthToken.key_thumbprint,
                    AuthToken.family_id,
                    AuthToken.authenticated_at,
                )
            )
            row = result.one_or_none()
            if row is None:
                replay_family = await session.scalar(
                    select(AuthToken.family_id).where(
                        AuthToken.token_digest == digest_secret(raw_refresh),
                        AuthToken.kind == "refresh",
                        AuthToken.epoch == epoch,
                        AuthToken.key_thumbprint == key_thumbprint,
                        AuthToken.rotated_at.is_not(None),
                    )
                )
                if replay_family is not None:
                    await session.execute(
                        update(AuthToken)
                        .where(
                            AuthToken.family_id == replay_family,
                            AuthToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=observed_at)
                    )
                await session.commit()
                return None
            access = await _insert_auth_token(
                session,
                raw_token=raw_access,
                kind="access",
                client_id=row[1],
                encoded_scopes=row[2],
                key_thumbprint=row[3],
                family_id=row[4],
                epoch=epoch,
                expires_at=access_expires_at,
                authenticated_at=row[5],
                now=observed_at,
            )
            refresh = await _insert_auth_token(
                session,
                raw_token=replacement_refresh,
                kind="refresh",
                client_id=row[1],
                encoded_scopes=row[2],
                key_thumbprint=row[3],
                family_id=row[4],
                parent_token_id=row[0],
                epoch=epoch,
                expires_at=refresh_expires_at,
                authenticated_at=row[5],
                now=observed_at,
            )
            if access is None or refresh is None:
                await session.rollback()
                return None
            await session.commit()
            return RotatedTokenPair(access_token=access, refresh_token=refresh)

    async def revoke(self, raw_token: str) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token_digest == digest_secret(raw_token),
                    AuthToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
                .returning(AuthToken.id)
            )
            revoked = result.scalar_one_or_none() is not None
            await session.commit()
            return revoked

    async def revoke_owned_family(
        self,
        raw_token: str,
        *,
        client_id: UUID,
        key_thumbprint: str,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            token = await session.scalar(
                select(AuthToken).where(
                    AuthToken.token_digest == digest_secret(raw_token),
                    AuthToken.client_id == client_id,
                    AuthToken.key_thumbprint == key_thumbprint,
                )
            )
            if token is None:
                return False
            family_condition = (
                AuthToken.family_id == token.family_id
                if token.family_id is not None
                else AuthToken.id == token.id
            )
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.client_id == client_id,
                    AuthToken.key_thumbprint == key_thumbprint,
                    family_condition,
                    AuthToken.revoked_at.is_(None),
                )
                .values(revoked_at=observed_at)
                .returning(AuthToken.id)
            )
            revoked = bool(result.scalars().all())
            await session.commit()
            return revoked


class AuthAuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        operation: str,
        result: str,
        source_digest: str,
        *,
        client_id: UUID | None = None,
        error_code: str | None = None,
    ) -> AuthAuditEvent:
        async with self._sessions() as session:
            event = AuthAuditEvent(
                operation=operation,
                result=result,
                source_digest=source_digest,
                client_id=client_id,
                error_code=error_code,
            )
            session.add(event)
            await session.commit()
            return event

    async def list_all(self) -> list[AuthAuditEvent]:
        async with self._sessions() as session:
            rows = await session.scalars(select(AuthAuditEvent).order_by(AuthAuditEvent.created_at))
            return list(rows)


class AgentToolRequestKeyConflict(RuntimeError):
    """The same side-effect request key was replayed with different arguments."""


class AgentProfileNameConflict(RuntimeError):
    """An Agent Profile display name is already in use."""


# Desired binding states that still occupy the one-profile/one-term slot.
# ``disabled`` remains selectable/configured (and therefore must prevent a
# duplicate binding); only terminal ``revoked`` rows are excluded.
_ACTIVE_BINDING_STATES = ("disabled", "enabled")


class AgentSetupReceiptRepository:
    """Durable setup idempotency receipts.

    ``claim_in_session`` is deliberately composable with the provisioning
    transaction.  The unique database key, rather than process-local state,
    arbitrates concurrent requests from independent workers.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_idempotency_key(self, idempotency_key: UUID) -> AgentSetupReceipt | None:
        async with self._sessions() as session:
            return cast(
                AgentSetupReceipt | None,
                await session.scalar(
                    select(AgentSetupReceipt).where(
                        AgentSetupReceipt.idempotency_key == idempotency_key
                    )
                ),
            )

    async def get_latest_for_term(self, term_id: UUID) -> AgentSetupReceipt | None:
        async with self._sessions() as session:
            return cast(
                AgentSetupReceipt | None,
                await session.scalar(
                    select(AgentSetupReceipt)
                    .where(AgentSetupReceipt.term_id == term_id)
                    .order_by(AgentSetupReceipt.created_at.desc())
                    .limit(1)
                ),
            )

    @staticmethod
    async def claim_in_session(
        session: AsyncSession,
        *,
        idempotency_key: UUID,
        request_digest: str,
        term_id: UUID,
    ) -> tuple[AgentSetupReceipt, bool]:
        """Claim a key, returning ``(receipt, is_owner)``.

        SQLite and PostgreSQL use their native ``ON CONFLICT DO NOTHING``
        forms.  The insert is the first write in the caller's transaction;
        therefore a losing concurrent request can safely roll back and read
        the committed winner without touching the aggregate.
        """

        values = {
            "id": uuid4(),
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "state": "activating",
            "term_id": term_id,
        }
        dialect = session.get_bind().dialect.name
        inserted_id: UUID | None = None
        statement: Any
        if dialect == "sqlite":
            statement = sqlite_insert(AgentSetupReceipt).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[AgentSetupReceipt.idempotency_key]
            )
            result = await session.execute(statement.returning(AgentSetupReceipt.id))
            inserted_id = result.scalar_one_or_none()
        elif dialect == "postgresql":
            statement = postgresql_insert(AgentSetupReceipt).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[AgentSetupReceipt.idempotency_key]
            )
            result = await session.execute(statement.returning(AgentSetupReceipt.id))
            inserted_id = result.scalar_one_or_none()
        else:
            # Keep a portable fallback for test/dialect adapters that do not
            # expose an ON CONFLICT builder.  This branch is only entered for
            # dialects without native support.
            candidate_receipt = AgentSetupReceipt(**values)
            try:
                session.add(candidate_receipt)
                await session.flush()
                return candidate_receipt, True
            except IntegrityError:
                await session.rollback()

        receipt: AgentSetupReceipt | None
        if inserted_id is not None:
            receipt = cast(
                AgentSetupReceipt | None,
                await session.get(AgentSetupReceipt, inserted_id),
            )
        else:
            receipt = cast(
                AgentSetupReceipt | None,
                await session.scalar(
                    select(AgentSetupReceipt).where(
                        AgentSetupReceipt.idempotency_key == idempotency_key
                    )
                ),
            )
        if receipt is None:
            # A conflict can only be reported after the winner committed.  A
            # missing row means the dialect violated that contract and must
            # not be treated as a successful claim.
            raise RuntimeError("setup receipt claim disappeared")
        return receipt, inserted_id is not None

    async def record_result(
        self,
        idempotency_key: UUID,
        *,
        state: str,
        binding_id: UUID | None,
        reason_code: str | None,
    ) -> AgentSetupReceipt | None:
        """Persist the public reconcile outcome exactly once."""

        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentSetupReceipt)
                .where(
                    AgentSetupReceipt.idempotency_key == idempotency_key,
                    AgentSetupReceipt.state == "activating",
                )
                .values(
                    state=state,
                    binding_id=binding_id,
                    reason_code=reason_code,
                    updated_at=observed_at,
                )
                .returning(AgentSetupReceipt)
            )
            receipt = result.scalar_one_or_none()
            if receipt is None:
                receipt = await session.scalar(
                    select(AgentSetupReceipt).where(
                        AgentSetupReceipt.idempotency_key == idempotency_key
                    )
                )
            await session.commit()
            return receipt


class AgentProfileRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        display_name: str,
        backend_kind: str,
        config: str,
    ) -> AgentProfile:
        async with self._sessions() as session:
            profile = AgentProfile(
                display_name=display_name,
                backend_kind=backend_kind,
                config=config,
            )
            session.add(profile)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise AgentProfileNameConflict from exc
            return profile

    async def get_by_id(self, profile_id: UUID) -> AgentProfile | None:
        async with self._sessions() as session:
            return await session.get(AgentProfile, profile_id)

    async def list(self) -> list[AgentProfile]:
        async with self._sessions() as session:
            rows = await session.scalars(select(AgentProfile).order_by(AgentProfile.created_at))
            return list(rows)

    async def rename(self, profile_id: UUID, display_name: str) -> AgentProfile | None:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentProfile)
                .where(AgentProfile.id == profile_id)
                .values(display_name=display_name, updated_at=observed_at)
                .returning(AgentProfile)
            )
            profile = result.scalar_one_or_none()
            await session.commit()
            return profile

    async def delete(self, profile_id: UUID) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                delete(AgentProfile).where(AgentProfile.id == profile_id).returning(AgentProfile.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()
            return deleted


class AgentBindingRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        profile_id: UUID,
        term_id: UUID,
        status: str = "disabled",
        runtime_ref: str | None = None,
        runtime_epoch: int | None = None,
        capability_ref: str | None = None,
    ) -> AgentBinding:
        async with self._sessions() as session:
            binding = AgentBinding(
                profile_id=profile_id,
                term_id=term_id,
                status=status,
                runtime_ref=runtime_ref,
                runtime_epoch=runtime_epoch,
                capability_ref=capability_ref,
            )
            session.add(binding)
            await session.commit()
            return binding

    async def get_by_id(self, binding_id: UUID) -> AgentBinding | None:
        async with self._sessions() as session:
            return await session.get(AgentBinding, binding_id)

    async def list_all(self) -> list[AgentBinding]:
        """Return every desired Binding for startup/periodic reconciliation."""

        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentBinding).order_by(
                    AgentBinding.created_at,
                    AgentBinding.id,
                )
            )
            return list(rows)

    async def get_by_runtime_ref(self, runtime_ref: str) -> AgentBinding | None:
        """Latest binding provisioned for a runtime ref (epoch resolution)."""
        async with self._sessions() as session:
            binding: AgentBinding | None = await session.scalar(
                select(AgentBinding)
                .where(AgentBinding.runtime_ref == runtime_ref)
                .order_by(AgentBinding.created_at.desc())
                .limit(1)
            )
            return binding

    async def list_for_term(self, term_id: UUID) -> list[AgentBinding]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentBinding)
                .where(AgentBinding.term_id == term_id)
                .order_by(AgentBinding.created_at)
            )
            return list(rows)

    async def list_for_profile(self, profile_id: UUID) -> list[AgentBinding]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentBinding)
                .where(AgentBinding.profile_id == profile_id)
                .order_by(AgentBinding.created_at)
            )
            return list(rows)

    async def active_binding_for(self, profile_id: UUID, term_id: UUID) -> AgentBinding | None:
        async with self._sessions() as session:
            binding: AgentBinding | None = await session.scalar(
                select(AgentBinding)
                .where(
                    AgentBinding.profile_id == profile_id,
                    AgentBinding.term_id == term_id,
                    AgentBinding.status.in_(_ACTIVE_BINDING_STATES),
                )
                .order_by(AgentBinding.created_at.desc())
                .limit(1)
            )
            return binding

    async def set_status(
        self,
        binding_id: UUID,
        status: str,
        *,
        expected_status: str | None = None,
        advance_runtime_epoch: bool = False,
    ) -> AgentBinding | None:
        observed_at = datetime.now(UTC)
        conditions = [AgentBinding.id == binding_id]
        if expected_status is not None:
            conditions.append(AgentBinding.status == expected_status)
        values: dict[str, object] = {
            "status": status,
            "updated_at": observed_at,
        }
        if advance_runtime_epoch:
            # Closing an open binding invalidates every epoch-bound AgentToken.
            # Repeating a closed-state patch is idempotent and nullable,
            # not-yet-provisioned runtime epochs stay null.
            values["runtime_epoch"] = case(
                (
                    ~AgentBinding.status.in_(("revoked", "disabled")),
                    AgentBinding.runtime_epoch + 1,
                ),
                else_=AgentBinding.runtime_epoch,
            )
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentBinding).where(*conditions).values(**values).returning(AgentBinding)
            )
            binding = result.scalar_one_or_none()
            await session.commit()
            return binding

    async def update_runtime(
        self,
        binding_id: UUID,
        *,
        runtime_ref: str,
        runtime_epoch: int,
        capability_ref: str,
    ) -> AgentBinding | None:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentBinding)
                .where(AgentBinding.id == binding_id)
                .values(
                    runtime_ref=runtime_ref,
                    runtime_epoch=runtime_epoch,
                    capability_ref=capability_ref,
                    updated_at=observed_at,
                )
                .returning(AgentBinding)
            )
            binding = result.scalar_one_or_none()
            await session.commit()
            return binding

    async def update_desired_runtime(
        self,
        binding_id: UUID,
        runtime_ref: str,
        capability_ref: str,
        expected_revision: int,
        rotate_epoch: bool,
    ) -> AgentBinding | None:
        """Atomically replace desired runtime configuration at one revision.

        Runtime/capability identity is authority-bearing.  A caller that does
        not request an epoch rotation may only advance the configuration
        revision while retaining the exact existing identity (for example, a
        model-only Profile change).  The revision predicate also makes a
        retry of a successful authority rotation a no-op.
        """

        if not runtime_ref or len(runtime_ref) > 128:
            raise ValueError("runtime_ref must be between 1 and 128 characters")
        if not capability_ref or len(capability_ref) > 128:
            raise ValueError("capability_ref must be between 1 and 128 characters")

        observed_at = datetime.now(UTC)
        conditions = [
            AgentBinding.id == binding_id,
            AgentBinding.config_revision == expected_revision,
            AgentBinding.status != "revoked",
        ]
        if not rotate_epoch:
            # These SQL predicates reject an authority-bearing identity change
            # in the same atomic statement; no read/check/write window exists.
            conditions.extend(
                (
                    AgentBinding.runtime_ref == runtime_ref,
                    AgentBinding.capability_ref == capability_ref,
                )
            )

        values: dict[str, object] = {
            "runtime_ref": runtime_ref,
            "capability_ref": capability_ref,
            "config_revision": AgentBinding.config_revision + 1,
            "updated_at": observed_at,
        }
        if rotate_epoch:
            values["runtime_epoch"] = func.coalesce(AgentBinding.runtime_epoch, 0) + 1

        async with self._sessions() as session:
            result = await session.execute(
                update(AgentBinding).where(*conditions).values(**values).returning(AgentBinding)
            )
            binding = result.scalar_one_or_none()
            await session.commit()
            return binding

    async def delete(self, binding_id: UUID) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                delete(AgentBinding).where(AgentBinding.id == binding_id).returning(AgentBinding.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()
            return deleted


class AgentMemoryScopeRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_for_binding(self, binding_id: UUID) -> AgentMemoryScope | None:
        async with self._sessions() as session:
            scope: AgentMemoryScope | None = await session.scalar(
                select(AgentMemoryScope).where(AgentMemoryScope.binding_id == binding_id)
            )
            return scope

    async def create(
        self,
        *,
        binding_id: UUID,
        term_id: UUID,
        profile_id: UUID,
        byte_quota: int,
        count_quota: int,
    ) -> AgentMemoryScope:
        async with self._sessions() as session:
            scope = AgentMemoryScope(
                binding_id=binding_id,
                term_id=term_id,
                profile_id=profile_id,
                byte_quota=byte_quota,
                count_quota=count_quota,
            )
            session.add(scope)
            await session.commit()
            return scope

    async def update_quota(
        self,
        scope_id: UUID,
        *,
        byte_quota: int,
        count_quota: int,
    ) -> AgentMemoryScope | None:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentMemoryScope)
                .where(AgentMemoryScope.id == scope_id)
                .values(
                    byte_quota=byte_quota,
                    count_quota=count_quota,
                    updated_at=observed_at,
                )
                .returning(AgentMemoryScope)
            )
            scope = result.scalar_one_or_none()
            await session.commit()
            return scope

    async def record_usage(
        self,
        scope_id: UUID,
        *,
        delta_bytes: int = 0,
        delta_count: int = 0,
    ) -> AgentMemoryScope | None:
        """Atomically adjust the live byte/count usage counters."""
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentMemoryScope)
                .where(AgentMemoryScope.id == scope_id)
                .values(
                    current_bytes=AgentMemoryScope.current_bytes + delta_bytes,
                    current_count=AgentMemoryScope.current_count + delta_count,
                    updated_at=observed_at,
                )
                .returning(AgentMemoryScope)
            )
            scope = result.scalar_one_or_none()
            await session.commit()
            return scope

    async def increment_usage(
        self,
        scope_id: UUID,
        *,
        delta_bytes: int = 0,
        delta_count: int = 0,
    ) -> AgentMemoryScope | None:
        """Alias for :meth:`record_usage`."""
        return await self.record_usage(scope_id, delta_bytes=delta_bytes, delta_count=delta_count)


class AgentConversationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        title: str | None = None,
        status: str = "active",
    ) -> AgentConversation:
        async with self._sessions() as session:
            conversation = AgentConversation(
                binding_id=binding_id,
                title=title,
                status=status,
            )
            session.add(conversation)
            await session.commit()
            return conversation

    async def get_by_id(self, conversation_id: UUID) -> AgentConversation | None:
        async with self._sessions() as session:
            return await session.get(AgentConversation, conversation_id)

    async def list_for_binding(
        self,
        binding_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentConversation]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentConversation)
                .where(AgentConversation.binding_id == binding_id)
                .order_by(AgentConversation.created_at)
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def rename(self, conversation_id: UUID, title: str) -> AgentConversation | None:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentConversation)
                .where(AgentConversation.id == conversation_id)
                .values(title=title, updated_at=observed_at)
                .returning(AgentConversation)
            )
            conversation = result.scalar_one_or_none()
            await session.commit()
            return conversation

    async def delete(self, conversation_id: UUID) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                delete(AgentConversation)
                .where(AgentConversation.id == conversation_id)
                .returning(AgentConversation.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()
            return deleted


class AgentBackendConversationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        conversation_id: UUID,
        backend_kind: str,
        runtime_id: str,
        binding_capability_epoch: int,
        provider_ref: str,
        backend_version: str | None = None,
    ) -> BackendConversationRef:
        async with self._sessions() as session:
            ref = BackendConversationRef(
                conversation_id=conversation_id,
                backend_kind=backend_kind,
                backend_version=backend_version,
                runtime_id=runtime_id,
                binding_capability_epoch=binding_capability_epoch,
                provider_ref=provider_ref,
            )
            session.add(ref)
            await session.commit()
            return ref

    async def get_by_conversation(self, conversation_id: UUID) -> BackendConversationRef | None:
        async with self._sessions() as session:
            ref: BackendConversationRef | None = await session.scalar(
                select(BackendConversationRef).where(
                    BackendConversationRef.conversation_id == conversation_id
                )
            )
            return ref

    async def get_by_provider_ref(self, provider_ref: str) -> BackendConversationRef | None:
        async with self._sessions() as session:
            ref: BackendConversationRef | None = await session.scalar(
                select(BackendConversationRef).where(
                    BackendConversationRef.provider_ref == provider_ref
                )
            )
            return ref

    async def delete(self, conversation_id: UUID) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                delete(BackendConversationRef)
                .where(BackendConversationRef.conversation_id == conversation_id)
                .returning(BackendConversationRef.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()
            return deleted


class AgentProviderDisclosureAcceptanceRepository:
    """Append-only provider disclosure acceptance history.

    Disclosure facts are inserted once and are never rewritten.  Revocation
    only stamps ``revoked_at`` so the original consent record remains
    auditable.  Deliberately, this API has no provider credential parameter.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        disclosure_fingerprint: str,
        provider_id: str,
        model_id: str,
        endpoint_origin: str,
        region: str,
        retention_terms: str,
        retention_version: str,
        no_training: bool,
        policy_version: str,
        accepted_at: datetime,
        accepted_auth_epoch: int,
        actor_kind: str,
        actor_ref: str,
    ) -> AgentProviderDisclosureAcceptance:
        async with self._sessions() as session:
            acceptance = AgentProviderDisclosureAcceptance(
                binding_id=binding_id,
                disclosure_fingerprint=disclosure_fingerprint,
                provider_id=provider_id,
                model_id=model_id,
                endpoint_origin=endpoint_origin,
                region=region,
                retention_terms=retention_terms,
                retention_version=retention_version,
                no_training=no_training,
                policy_version=policy_version,
                accepted_at=accepted_at,
                accepted_auth_epoch=accepted_auth_epoch,
                actor_kind=actor_kind,
                actor_ref=actor_ref,
            )
            session.add(acceptance)
            await session.commit()
            return acceptance

    async def get_current(
        self,
        binding_id: UUID,
        disclosure_fingerprint: str,
    ) -> AgentProviderDisclosureAcceptance | None:
        async with self._sessions() as session:
            acceptance: AgentProviderDisclosureAcceptance | None = await session.scalar(
                select(AgentProviderDisclosureAcceptance)
                .where(
                    AgentProviderDisclosureAcceptance.binding_id == binding_id,
                    AgentProviderDisclosureAcceptance.disclosure_fingerprint
                    == disclosure_fingerprint,
                    AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                )
                .order_by(
                    AgentProviderDisclosureAcceptance.accepted_at.desc(),
                    AgentProviderDisclosureAcceptance.id.desc(),
                )
                .limit(1)
            )
            return acceptance

    async def revoke_all_current(
        self,
        binding_id: UUID,
        *,
        now: datetime | None = None,
    ) -> int:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(AgentProviderDisclosureAcceptance)
                    .where(
                        AgentProviderDisclosureAcceptance.binding_id == binding_id,
                        AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                    )
                    .values(revoked_at=observed_at)
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)


class AgentRuntimeBindingRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert(
        self,
        *,
        binding_id: UUID,
        runtime_ref: str,
        runtime_epoch: int,
        readiness: str = "not_ready",
    ) -> AgentRuntimeBinding:
        if readiness == "ready":
            raise ValueError("ready may only be written by compare_and_set_ready")
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            runtime = await session.scalar(
                select(AgentRuntimeBinding).where(
                    AgentRuntimeBinding.binding_id == binding_id,
                )
            )
            if runtime is None:
                runtime = AgentRuntimeBinding(
                    binding_id=binding_id,
                    runtime_ref=runtime_ref,
                    runtime_epoch=runtime_epoch,
                    readiness=readiness,
                )
                session.add(runtime)
            else:
                runtime.runtime_ref = runtime_ref
                runtime.runtime_epoch = runtime_epoch
                runtime.readiness = readiness
                runtime.updated_at = observed_at
            await session.commit()
            return runtime

    async def get_by_binding(self, binding_id: UUID) -> AgentRuntimeBinding | None:
        async with self._sessions() as session:
            runtime: AgentRuntimeBinding | None = await session.scalar(
                select(AgentRuntimeBinding).where(AgentRuntimeBinding.binding_id == binding_id)
            )
            return runtime

    async def set_readiness(
        self, runtime_binding_id: UUID, readiness: str
    ) -> AgentRuntimeBinding | None:
        if readiness == "ready":
            raise ValueError("ready may only be written by compare_and_set_ready")
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(AgentRuntimeBinding.id == runtime_binding_id)
                .values(readiness=readiness, updated_at=observed_at)
                .returning(AgentRuntimeBinding)
            )
            runtime = result.scalar_one_or_none()
            await session.commit()
            return runtime

    async def mark_reconciling(
        self,
        binding_id: UUID,
        expected_revision: int,
        fingerprint: str,
    ) -> AgentRuntimeBinding | None:
        """Conditionally upsert one reconciling row for an enabled Binding.

        The dialect-native conflict clause makes concurrent first reconcile
        attempts converge on the unique ``binding_id`` row.  Both the INSERT
        source and the conflict UPDATE re-check enabled state and revision in
        the database.  Applied runtime identity columns are intentionally
        absent from the update set.
        """

        observed_at = datetime.now(UTC)
        desired_matches = exists(
            select(AgentBinding.id).where(
                AgentBinding.id == binding_id,
                AgentBinding.status.in_(("enabled", "pending", "ready")),
                AgentBinding.config_revision == expected_revision,
            )
        )
        insert_columns = (
            "id",
            "binding_id",
            "readiness",
            "reason_code",
            "config_fingerprint",
            "transition_started_at",
            "provider_readiness",
            "created_at",
            "updated_at",
        )
        source = select(
            literal(uuid4(), type_=AgentRuntimeBinding.id.type),
            AgentBinding.id,
            literal("reconciling"),
            literal(None, type_=AgentRuntimeBinding.reason_code.type),
            literal(fingerprint),
            literal(observed_at, type_=AgentRuntimeBinding.transition_started_at.type),
            literal("configured_unverified"),
            literal(observed_at, type_=AgentRuntimeBinding.created_at.type),
            literal(observed_at, type_=AgentRuntimeBinding.updated_at.type),
        ).where(
            AgentBinding.id == binding_id,
            AgentBinding.status.in_(("enabled", "pending", "ready")),
            AgentBinding.config_revision == expected_revision,
        )

        async with self._sessions() as session:
            bind = session.get_bind()
            statement: Any
            if bind.dialect.name == "sqlite":
                statement = sqlite_insert(AgentRuntimeBinding).from_select(
                    insert_columns,
                    source,
                )
            elif bind.dialect.name == "postgresql":
                statement = postgresql_insert(AgentRuntimeBinding).from_select(
                    insert_columns,
                    source,
                )
            else:
                raise RuntimeError("unsupported database dialect for runtime upsert")
            statement = statement.on_conflict_do_update(
                index_elements=[AgentRuntimeBinding.binding_id],
                set_={
                    "readiness": "reconciling",
                    "reason_code": None,
                    "config_fingerprint": fingerprint,
                    "transition_started_at": observed_at,
                    "provider_readiness": "configured_unverified",
                    "provider_verified_revision": None,
                    "provider_last_checked_at": None,
                    "provider_reason_code": None,
                    "updated_at": observed_at,
                },
                where=desired_matches,
            ).returning(AgentRuntimeBinding)
            result = await session.execute(statement)
            runtime = result.scalar_one_or_none()
            await session.commit()
            return runtime

    async def compare_and_set_ready(
        self,
        binding_id: UUID,
        expected_revision: int,
        fingerprint: str,
        observed_epoch: int,
    ) -> bool:
        """Publish observed ready state through one final atomic CAS."""

        observed_at = datetime.now(UTC)
        desired_matches = exists(
            select(AgentBinding.id).where(
                AgentBinding.id == binding_id,
                AgentBinding.status.in_(("enabled", "pending", "ready")),
                AgentBinding.config_revision == expected_revision,
                AgentBinding.runtime_epoch == observed_epoch,
                AgentBinding.runtime_ref.is_not(None),
                AgentBinding.capability_ref.is_not(None),
            )
        )
        disclosure_matches = exists(
            select(AgentProviderDisclosureAcceptance.id).where(
                AgentProviderDisclosureAcceptance.binding_id == binding_id,
                AgentProviderDisclosureAcceptance.disclosure_fingerprint == fingerprint,
                AgentProviderDisclosureAcceptance.revoked_at.is_(None),
            )
        )
        desired_runtime_ref = (
            select(AgentBinding.runtime_ref).where(AgentBinding.id == binding_id).scalar_subquery()
        )
        desired_capability_ref = (
            select(AgentBinding.capability_ref)
            .where(AgentBinding.id == binding_id)
            .scalar_subquery()
        )

        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(
                    AgentRuntimeBinding.binding_id == binding_id,
                    AgentRuntimeBinding.readiness == "reconciling",
                    AgentRuntimeBinding.config_fingerprint == fingerprint,
                    desired_matches,
                    disclosure_matches,
                )
                .values(
                    readiness="ready",
                    reason_code=None,
                    observed_runtime_ref=desired_runtime_ref,
                    observed_runtime_epoch=observed_epoch,
                    observed_capability_ref=desired_capability_ref,
                    applied_revision=expected_revision,
                    last_health_at=observed_at,
                    updated_at=observed_at,
                )
                .returning(AgentRuntimeBinding.id)
            )
            changed = result.scalar_one_or_none() is not None
            await session.commit()
            return changed

    async def compare_and_set_provider_verified(
        self,
        binding_id: UUID,
        expected_revision: int,
        *,
        fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record one successful provider response for the current revision.

        Runtime readiness and provider readiness are deliberately independent.
        This CAS only succeeds when the desired Binding and the observed
        runtime still point at ``expected_revision``.  A late event from a
        stopped pipeline therefore cannot certify a newer configuration.
        """

        observed_at = now or datetime.now(UTC)
        desired_matches = exists(
            select(AgentBinding.id).where(
                AgentBinding.id == binding_id,
                AgentBinding.status.in_(("enabled", "pending", "ready")),
                AgentBinding.config_revision == expected_revision,
            )
        )
        conditions = [
            AgentRuntimeBinding.binding_id == binding_id,
            AgentRuntimeBinding.readiness == "ready",
            AgentRuntimeBinding.applied_revision == expected_revision,
            desired_matches,
        ]
        if fingerprint is not None:
            conditions.append(AgentRuntimeBinding.config_fingerprint == fingerprint)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(*conditions)
                .values(
                    provider_readiness="verified",
                    provider_verified_revision=expected_revision,
                    provider_last_checked_at=observed_at,
                    provider_reason_code=None,
                    updated_at=observed_at,
                )
                .returning(AgentRuntimeBinding.id)
            )
            changed = result.scalar_one_or_none() is not None
            await session.commit()
            return changed

    async def mark_provider_verified(
        self,
        binding_id: UUID,
        expected_revision: int,
        *,
        fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Compatibility/readability alias for the provider verification CAS."""

        return await self.compare_and_set_provider_verified(
            binding_id,
            expected_revision,
            fingerprint=fingerprint,
            now=now,
        )

    async def compare_and_set_provider_failed(
        self,
        binding_id: UUID,
        expected_revision: int,
        reason_code: str,
        *,
        fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record a classified provider rejection without storing its detail."""

        if reason_code not in {"provider_auth_failed", "provider_model_rejected"}:
            raise ValueError("unsupported provider failure reason")
        observed_at = now or datetime.now(UTC)
        desired_matches = exists(
            select(AgentBinding.id).where(
                AgentBinding.id == binding_id,
                AgentBinding.status.in_(("enabled", "pending", "ready")),
                AgentBinding.config_revision == expected_revision,
            )
        )
        conditions = [
            AgentRuntimeBinding.binding_id == binding_id,
            AgentRuntimeBinding.applied_revision == expected_revision,
            desired_matches,
        ]
        if fingerprint is not None:
            conditions.append(AgentRuntimeBinding.config_fingerprint == fingerprint)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(*conditions)
                .values(
                    provider_readiness="failed",
                    provider_verified_revision=None,
                    provider_last_checked_at=observed_at,
                    provider_reason_code=reason_code,
                    updated_at=observed_at,
                )
                .returning(AgentRuntimeBinding.id)
            )
            changed = result.scalar_one_or_none() is not None
            await session.commit()
            return changed

    async def mark_provider_failed(
        self,
        binding_id: UUID,
        expected_revision: int,
        reason_code: str,
        *,
        fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Compatibility/readability alias for the provider failure CAS."""

        return await self.compare_and_set_provider_failed(
            binding_id,
            expected_revision,
            reason_code,
            fingerprint=fingerprint,
            now=now,
        )

    async def mark_unavailable(
        self,
        binding_id: UUID,
        readiness: str,
        reason_code: str | None,
    ) -> AgentRuntimeBinding | None:
        if readiness == "ready":
            raise ValueError("ready may only be written by compare_and_set_ready")
        if readiness not in {"unprovisioned", "not_ready", "blocked", "disabled"}:
            raise ValueError("unsupported unavailable readiness")

        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(AgentRuntimeBinding.binding_id == binding_id)
                .values(
                    readiness=readiness,
                    reason_code=reason_code,
                    updated_at=observed_at,
                )
                .returning(AgentRuntimeBinding)
            )
            runtime = result.scalar_one_or_none()
            await session.commit()
            return runtime

    async def record_health(
        self,
        runtime_binding_id: UUID,
        *,
        now: datetime | None = None,
    ) -> AgentRuntimeBinding | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeBinding)
                .where(AgentRuntimeBinding.id == runtime_binding_id)
                .values(last_health_at=observed_at, updated_at=observed_at)
                .returning(AgentRuntimeBinding)
            )
            runtime = result.scalar_one_or_none()
            await session.commit()
            return runtime


class AgentInboxRepository:
    """Durable typed Agent input queue (plan §4.2, §7).

    ``admission_seq`` is assigned by the database transaction, never by a
    client clock.  The assignment uses a single atomic ``INSERT ... SELECT``
    statement that computes ``COALESCE(MAX(admission_seq), 0) + 1`` for the
    conversation inside the insert itself.  Because the statement reads and
    writes in one atomic step and SQLite serializes writers on the database
    write lock, concurrent enqueues for the same conversation can never
    produce a duplicate or a gap in ``admission_seq``; the unique
    ``(conversation_id, admission_seq)`` constraint is the last line of
    defense.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enqueue(
        self,
        *,
        conversation_id: UUID,
        kind: str,
        actor_id: str,
        actor_kind: str,
        idempotency_key: str,
        payload_digest: str,
        source: str,
        auth_epoch: int | None = None,
        causation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> AgentInboxItem:
        observed_at = now or datetime.now(UTC)
        effective_correlation_id = correlation_id or uuid4()
        async with self._sessions() as session:
            item = await self._insert_pending(
                session,
                conversation_id=conversation_id,
                kind=kind,
                actor_id=actor_id,
                actor_kind=actor_kind,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                source=source,
                auth_epoch=auth_epoch,
                causation_id=causation_id,
                correlation_id=effective_correlation_id,
                now=observed_at,
            )
            await session.commit()
            return item

    @staticmethod
    async def _insert_pending(
        session: AsyncSession,
        *,
        conversation_id: UUID,
        kind: str,
        actor_id: str,
        actor_kind: str,
        idempotency_key: str,
        payload_digest: str,
        source: str,
        auth_epoch: int | None = None,
        causation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> AgentInboxItem:
        """Insert one pending inbox item inside the caller's transaction.

        The DB-assigned ``admission_seq`` is computed atomically inside the
        ``INSERT ... SELECT`` statement (see :meth:`enqueue`); the helper
        never commits so callers can compose it with other writes (e.g. the
        draft consumption CAS) in one transaction.
        """
        observed_at = now or datetime.now(UTC)
        effective_correlation_id = correlation_id or uuid4()
        result = await session.execute(
            insert(AgentInboxItem)
            .from_select(
                [
                    AgentInboxItem.conversation_id,
                    AgentInboxItem.kind,
                    AgentInboxItem.actor_id,
                    AgentInboxItem.actor_kind,
                    AgentInboxItem.auth_epoch,
                    AgentInboxItem.admission_seq,
                    AgentInboxItem.idempotency_key,
                    AgentInboxItem.payload_digest,
                    AgentInboxItem.source,
                    AgentInboxItem.delivery_state,
                    AgentInboxItem.attempt_count,
                    AgentInboxItem.claim_owner,
                    AgentInboxItem.claim_expires_at,
                    AgentInboxItem.next_attempt_at,
                    AgentInboxItem.causation_id,
                    AgentInboxItem.correlation_id,
                    AgentInboxItem.created_at,
                ],
                select(
                    literal(conversation_id),
                    literal(kind),
                    literal(actor_id),
                    literal(actor_kind),
                    literal(auth_epoch),
                    func.coalesce(func.max(AgentInboxItem.admission_seq), 0) + 1,
                    literal(idempotency_key),
                    literal(payload_digest),
                    literal(source),
                    literal("pending"),
                    literal(0),
                    literal(None),
                    literal(None),
                    literal(observed_at),
                    literal(causation_id),
                    literal(effective_correlation_id),
                    literal(observed_at),
                ).where(AgentInboxItem.conversation_id == conversation_id),
            )
            .returning(AgentInboxItem)
        )
        return result.scalar_one()

    async def next_pending(
        self,
        *,
        conversation_id: UUID | None = None,
        limit: int = 10,
        now: datetime | None = None,
        after: tuple[UUID, int] | None = None,
    ) -> list[AgentInboxItem]:
        """Return claimable items using the composite claims index.

        An item is claimable when it is pending/retry-waiting/claimed, its
        retry deadline has passed, and either it has no claim lease or the
        lease has expired.  An expired ``claimed`` item is intentionally
        returned so a crashed worker's work can be reclaimed.

        ``after`` is a keyset cursor ``(conversation_id, admission_seq)``
        matching the result order: only rows strictly after the cursor are
        returned, so callers can page past a window fully blocked by a
        claim filter instead of starving later rows (review fix major-1).
        """
        observed_at = now or datetime.now(UTC)
        conditions = [
            AgentInboxItem.delivery_state.in_(("pending", "retry_wait", "claimed")),
            AgentInboxItem.next_attempt_at <= observed_at,
            or_(
                AgentInboxItem.claim_expires_at.is_(None),
                AgentInboxItem.claim_expires_at <= observed_at,
            ),
        ]
        if conversation_id is not None:
            conditions.append(AgentInboxItem.conversation_id == conversation_id)
        if after is not None:
            after_conversation_id, after_admission_seq = after
            conditions.append(
                or_(
                    AgentInboxItem.conversation_id > after_conversation_id,
                    and_(
                        AgentInboxItem.conversation_id == after_conversation_id,
                        AgentInboxItem.admission_seq > after_admission_seq,
                    ),
                )
            )
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentInboxItem)
                .where(*conditions)
                .order_by(
                    AgentInboxItem.conversation_id,
                    AgentInboxItem.admission_seq,
                )
                .limit(limit)
            )
            return list(rows)

    async def next_pending_for_binding(
        self,
        binding_id: UUID,
        *,
        limit: int = 10,
        now: datetime | None = None,
        after: tuple[int, UUID] | None = None,
    ) -> list[AgentInboxItem]:
        """Return claimable items for one binding's conversations (M4.5 §2).

        The shared inbox is not binding-scoped, so the claim candidates are
        filtered by joining the conversation's binding (M0 isolation).  This
        replaces the previous ``list_for_binding`` (default limit 50) scan:
        the 51st+ conversation's items were silently starved by that page.
        Claimability mirrors :meth:`next_pending`; the ordering puts the
        lowest admission sequence first so a batch can never re-order within
        a conversation.

        ``after`` is a keyset cursor ``(admission_seq, conversation_id)``
        matching the result order: only rows strictly after the cursor are
        returned, so callers can page past a window fully blocked by the
        claim filter instead of starving later rows (review fix major-1).
        """
        observed_at = now or datetime.now(UTC)
        conditions = [
            AgentConversation.binding_id == binding_id,
            AgentInboxItem.delivery_state.in_(("pending", "retry_wait", "claimed")),
            AgentInboxItem.next_attempt_at <= observed_at,
            or_(
                AgentInboxItem.claim_expires_at.is_(None),
                AgentInboxItem.claim_expires_at <= observed_at,
            ),
        ]
        if after is not None:
            after_admission_seq, after_conversation_id = after
            conditions.append(
                or_(
                    AgentInboxItem.admission_seq > after_admission_seq,
                    and_(
                        AgentInboxItem.admission_seq == after_admission_seq,
                        AgentInboxItem.conversation_id > after_conversation_id,
                    ),
                )
            )
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentInboxItem)
                .join(
                    AgentConversation,
                    AgentConversation.id == AgentInboxItem.conversation_id,
                )
                .where(*conditions)
                .order_by(
                    AgentInboxItem.admission_seq,
                    AgentInboxItem.conversation_id,
                )
                .limit(limit)
            )
            return list(rows)

    async def claim(
        self,
        item_id: UUID,
        owner: str,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> AgentInboxItem | None:
        """CAS-claim an item under an owner lease (fencing token).

        ``owner`` is the unique fencing token for this claim attempt; only
        that owner may later dispatch the item while the lease is live.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state.in_(("pending", "retry_wait", "claimed")),
                    AgentInboxItem.next_attempt_at <= observed_at,
                    or_(
                        AgentInboxItem.claim_expires_at.is_(None),
                        AgentInboxItem.claim_expires_at <= observed_at,
                    ),
                )
                .values(
                    delivery_state="claimed",
                    claim_owner=owner,
                    claim_expires_at=observed_at + timedelta(seconds=lease_seconds),
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def mark_dispatched(
        self,
        item_id: UUID,
        owner: str,
        *,
        now: datetime | None = None,
    ) -> AgentInboxItem | None:
        """Fence the dispatch on the live claim: only the lease holder can mark
        the item dispatched before the lease expires.

        The claim owner and lease are deliberately kept on the dispatched row:
        they double as the submission's persisted fencing identity and
        submission lease, so restart recovery can fence a started submission
        whose outcome is unproven (plan §7/§17).  The dispatched state is
        never re-claimed by the claim scan.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state == "claimed",
                    AgentInboxItem.claim_owner == owner,
                    AgentInboxItem.claim_expires_at > observed_at,
                )
                .values(
                    delivery_state="dispatched",
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def start_submission(
        self,
        item_id: UUID,
        owner: str,
        *,
        attempt_id: str,
        fencing_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> AgentInboxItem | None:
        """Commit ``submission_state=started`` before any network write (plan §7).

        The CAS succeeds only while the caller still holds the live claim.
        The started row persists the attempt/fencing identity as
        ``claim_owner = "<attempt_id>|<fencing_token>"`` and a fresh
        submission lease (``claim_expires_at``) so restart recovery can fence
        or surface the started submission without any in-memory state.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state == "claimed",
                    AgentInboxItem.claim_owner == owner,
                    AgentInboxItem.claim_expires_at > observed_at,
                )
                .values(
                    delivery_state="dispatched",
                    claim_owner=f"{attempt_id}|{fencing_token}",
                    claim_expires_at=observed_at + timedelta(seconds=lease_seconds),
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def mark_delivered(
        self,
        item_id: UUID,
    ) -> AgentInboxItem | None:
        """Prove the delivery accepted: ``dispatched`` -> ``delivered``.

        ``delivered`` is a persisted terminal state: the one-in-flight gate
        stops counting the item, freeing the conversation's next turn
        (plan §2.1 multi-turn conversations).
        """
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state == "dispatched",
                )
                .values(
                    delivery_state="delivered",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def mark_delivery_unknown(
        self,
        item_id: UUID,
    ) -> AgentInboxItem | None:
        """Persist ``delivery_unknown``: uncertain delivery is a visible
        recoverable state, never an automatic duplicate action (plan §7/§17)."""
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state.in_(
                        ("claimed", "dispatched", "cancel_requested")
                    ),
                )
                .values(
                    delivery_state="delivery_unknown",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def mark_cancel_requested(
        self,
        item_id: UUID,
    ) -> AgentInboxItem | None:
        """``dispatched`` -> ``cancel_requested`` before the backend cancel call.

        The submission lease is kept so recovery can fence a cancellation that
        died mid-flight; the terminal outcome is ``cancelled`` or
        ``delivery_unknown`` (plan §7: ``dispatched -> cancel_requested ->
        cancelled|unknown``).
        """
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state == "dispatched",
                )
                .values(delivery_state="cancel_requested")
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def list_started_with_expired_lease(
        self,
        *,
        now: datetime | None = None,
    ) -> list[AgentInboxItem]:
        """Started/cancel-requested submissions whose lease expired without a
        proven terminal outcome (restart recovery, plan §17).

        These rows are provably past their submission lease but their outcome
        is unproven, so recovery must fence them to ``delivered`` (reconciled)
        or ``delivery_unknown`` instead of re-offering them for delivery.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentInboxItem)
                .where(
                    AgentInboxItem.delivery_state.in_(("dispatched", "cancel_requested")),
                    AgentInboxItem.claim_expires_at.is_not(None),
                    AgentInboxItem.claim_expires_at <= observed_at,
                )
                .order_by(AgentInboxItem.admission_seq)
            )
            return list(rows)

    async def mark_retry(
        self,
        item_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> AgentInboxItem | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state.in_(("pending", "claimed", "retry_wait")),
                )
                .values(
                    delivery_state="retry_wait",
                    attempt_count=AgentInboxItem.attempt_count + 1,
                    next_attempt_at=next_attempt_at,
                    claim_owner=None,
                    claim_expires_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def dead_letter(
        self,
        item_id: UUID,
    ) -> AgentInboxItem | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state.in_(("pending", "claimed", "retry_wait")),
                )
                .values(
                    delivery_state="dead_letter",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def mark_cancelled(
        self,
        item_id: UUID,
    ) -> AgentInboxItem | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentInboxItem)
                .where(
                    AgentInboxItem.id == item_id,
                    AgentInboxItem.delivery_state.in_(
                        ("pending", "claimed", "retry_wait", "cancel_requested")
                    ),
                )
                .values(
                    delivery_state="cancelled",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=None,
                )
                .returning(AgentInboxItem)
            )
            item = result.scalar_one_or_none()
            await session.commit()
            return item

    async def get_by_idempotency_key(self, idempotency_key: str) -> AgentInboxItem | None:
        async with self._sessions() as session:
            item: AgentInboxItem | None = await session.scalar(
                select(AgentInboxItem).where(AgentInboxItem.idempotency_key == idempotency_key)
            )
            return item

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentInboxItem]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentInboxItem)
                .where(AgentInboxItem.conversation_id == conversation_id)
                .order_by(AgentInboxItem.admission_seq)
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def purge_terminal(
        self,
        *,
        now: datetime | None = None,
        older_than: timedelta = AGENT_TERMINAL_RETENTION,
    ) -> int:
        """Delete terminal-state rows after the retention window (plan §15).

        ``delivered``, ``delivery_unknown``, ``dead_letter``, and ``cancelled``
        deliveries are terminal recovery states; their admission rows are
        retained for the configured window and then removed by the startup
        purge sweep.
        """
        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - older_than
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentInboxItem).where(
                        AgentInboxItem.delivery_state.in_(
                            (
                                "dead_letter",
                                "cancelled",
                                "delivered",
                                "delivery_unknown",
                            )
                        ),
                        AgentInboxItem.created_at <= cutoff,
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class AgentToolRequestRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        tool_call_id: str,
        request_key: str,
        arguments_digest: str,
        run_id: UUID | None = None,
    ) -> AgentToolRequest:
        async with self._sessions() as session:
            request = AgentToolRequest(
                binding_id=binding_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                request_key=request_key,
                arguments_digest=arguments_digest,
            )
            session.add(request)
            await session.commit()
            return request

    async def get_by_key(self, request_key: str) -> AgentToolRequest | None:
        async with self._sessions() as session:
            request: AgentToolRequest | None = await session.scalar(
                select(AgentToolRequest).where(AgentToolRequest.request_key == request_key)
            )
            return request

    async def record_result(self, request_key: str, result_digest: str) -> AgentToolRequest | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentToolRequest)
                .where(AgentToolRequest.request_key == request_key)
                .values(recorded_result_digest=result_digest)
                .returning(AgentToolRequest)
            )
            request = result.scalar_one_or_none()
            await session.commit()
            return request

    async def reject_duplicate_key_different_args(
        self, request_key: str, arguments_digest: str
    ) -> AgentToolRequest | None:
        """Guard a side-effect replay: the request key is single-use for its
        canonical arguments hash.  Same key + same digest returns the recorded
        row; same key + a different digest is rejected."""
        async with self._sessions() as session:
            request: AgentToolRequest | None = await session.scalar(
                select(AgentToolRequest).where(AgentToolRequest.request_key == request_key)
            )
            if request is None:
                return None
            if request.arguments_digest != arguments_digest:
                raise AgentToolRequestKeyConflict(request_key)
            return request


class AgentRunRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        conversation_id: UUID,
        run_state: str = "queued",
        started_at: datetime | None = None,
        backend_run_id: str | None = None,
    ) -> AgentRun:
        observed_at = started_at or datetime.now(UTC)
        async with self._sessions() as session:
            run = AgentRun(
                conversation_id=conversation_id,
                run_state=run_state,
                started_at=observed_at,
                backend_run_id=backend_run_id,
            )
            session.add(run)
            await session.commit()
            return run

    async def get_by_id(self, run_id: UUID) -> AgentRun | None:
        async with self._sessions() as session:
            return await session.get(AgentRun, run_id)

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRun]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentRun)
                .where(AgentRun.conversation_id == conversation_id)
                .order_by(AgentRun.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def list_by_state(self, run_state: str) -> list[AgentRun]:
        """Return every run currently in one state, oldest first.

        Used by restart recovery to fence runs that were mid-flight when the
        process stopped (plan §17: B restarts).
        """
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentRun)
                .where(AgentRun.run_state == run_state)
                .order_by(AgentRun.started_at)
            )
            return list(rows)

    async def list_active_for_binding(self, binding_id: UUID) -> list[AgentRun]:
        """Return queued/running runs for one binding's conversations (M4.5 §5).

        The disconnect reconcile loop previously iterated
        ``AgentConversationRepository.list_for_binding`` (default limit 50),
        so an active run on the 51st+ conversation was never reconciled.
        This join is unbounded and binding-scoped instead.
        """
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentRun)
                .join(
                    AgentConversation,
                    AgentConversation.id == AgentRun.conversation_id,
                )
                .where(
                    AgentConversation.binding_id == binding_id,
                    AgentRun.run_state.in_(("queued", "running")),
                )
                .order_by(AgentRun.started_at)
            )
            return list(rows)

    async def purge_terminal(
        self,
        *,
        now: datetime | None = None,
        older_than: timedelta = AGENT_TERMINAL_RETENTION,
    ) -> int:
        """Delete terminal-state runs after the retention window (plan §15).

        ``completed``/``failed``/``cancelled``/``unknown`` runs are terminal;
        their rows are retained for the configured window and then removed by
        the startup purge sweep.
        """
        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - older_than
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentRun).where(
                        AgentRun.run_state.in_(("completed", "failed", "cancelled", "unknown")),
                        AgentRun.completed_at <= cutoff,
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count

    async def set_state(
        self,
        run_id: UUID,
        new_state: str,
        *,
        expected_state: str | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> AgentRun | None:
        observed_at = now or datetime.now(UTC)
        conditions = [AgentRun.id == run_id]
        if expected_state is not None:
            conditions.append(AgentRun.run_state == expected_state)
        values: dict[str, object] = {"run_state": new_state}
        if new_state in ("completed", "failed", "cancelled", "unknown"):
            values["completed_at"] = observed_at
        if error_code is not None:
            values["error_code"] = error_code
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRun).where(*conditions).values(**values).returning(AgentRun)
            )
            run = result.scalar_one_or_none()
            await session.commit()
            return run

    async def set_backend_run_id(self, run_id: UUID, backend_run_id: str) -> AgentRun | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(backend_run_id=backend_run_id)
                .returning(AgentRun)
            )
            run = result.scalar_one_or_none()
            await session.commit()
            return run


class AgentMessageRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        conversation_id: UUID,
        role: str,
        kind: str,
        body_digest: str,
        run_id: UUID | None = None,
        is_final: bool = False,
        assembly_revision: int | None = None,
        body: str | None = None,
    ) -> AgentMessage:
        if body is not None:
            # Body bounds and digest invariant (M6b spec §6.6): storage and
            # hash are the same byte string, so the digest stays verifiable
            # and body/digest drift fails fast instead of silently rendering
            # the wrong historical user text.
            body_bytes = body.encode("utf-8")
            if len(body_bytes) > MAX_AGENT_MESSAGE_BODY_BYTES:
                raise ValueError(
                    f"message body must not exceed {MAX_AGENT_MESSAGE_BODY_BYTES} bytes (64 KiB)"
                )
            if hashlib.sha256(body_bytes).hexdigest() != body_digest:
                raise ValueError("body_digest must equal sha256(body) when a body is provided")
        observed_at = datetime.now(UTC)
        if assembly_revision is None:
            # Auto-assign the next revision atomically: the aggregate subquery
            # reads the conversation's current maximum inside the same statement
            # that inserts, so the revision is DB-assigned and monotonic per
            # conversation.
            source = (
                select(
                    literal(conversation_id),
                    literal(run_id),
                    literal(role),
                    literal(kind),
                    func.coalesce(func.max(AgentMessage.assembly_revision), 0) + 1,
                    literal(is_final),
                    literal(body_digest),
                    literal(body),
                    literal(observed_at),
                )
                .where(AgentMessage.conversation_id == conversation_id)
                .limit(1)
            )
        else:
            # Caller-supplied revision: a literal SELECT without a FROM clause
            # always yields exactly one row, including for an empty conversation.
            source = select(
                literal(conversation_id),
                literal(run_id),
                literal(role),
                literal(kind),
                literal(assembly_revision),
                literal(is_final),
                literal(body_digest),
                literal(body),
                literal(observed_at),
            )
        async with self._sessions() as session:
            result = await session.execute(
                insert(AgentMessage)
                .from_select(
                    [
                        AgentMessage.conversation_id,
                        AgentMessage.run_id,
                        AgentMessage.role,
                        AgentMessage.kind,
                        AgentMessage.assembly_revision,
                        AgentMessage.is_final,
                        AgentMessage.body_digest,
                        AgentMessage.body,
                        AgentMessage.created_at,
                    ],
                    source,
                )
                .returning(AgentMessage)
            )
            message = result.scalar_one()
            await session.commit()
            return message

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentMessage]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentMessage)
                .where(AgentMessage.conversation_id == conversation_id)
                .order_by(AgentMessage.assembly_revision)
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def latest_revision(self, conversation_id: UUID) -> int | None:
        async with self._sessions() as session:
            revision = await session.scalar(
                select(func.max(AgentMessage.assembly_revision)).where(
                    AgentMessage.conversation_id == conversation_id
                )
            )
            return int(revision) if revision is not None else None

    async def find_user_message_by_digest(
        self, conversation_id: UUID, body_digest: str
    ) -> AgentMessage | None:
        """Return the newest persisted user message whose body hashes to
        ``body_digest`` (digest-verified payload recovery, review fix).

        The inbox item's ``payload_digest`` is ``sha256(text)`` for user
        messages and the message row's ``body_digest`` carries the same
        hash, so a digest match proves the re-submission text without any
        cross-table linkage.
        """
        async with self._sessions() as session:
            row = await session.scalar(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.role == "user",
                    AgentMessage.body_digest == body_digest,
                )
                .order_by(AgentMessage.assembly_revision.desc())
                .limit(1)
            )
            return row

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete message rows past their §16.1 retention ceiling.

        Non-final assembly checkpoints follow the 24-hour
        ``ASSEMBLY_CHECKPOINTS`` ceiling; final product messages follow the
        30-day ``FINAL_MESSAGES`` ceiling.  Both ceilings are wired from
        ``RETENTION_MATRIX`` via :data:`AGENT_ASSEMBLY_CHECKPOINT_RETENTION`
        and :data:`AGENT_FINAL_TIMELINE_RETENTION` (plan §15 startup purge
        sweep, §16.1 retention defaults).
        """
        observed_at = now or datetime.now(UTC)
        checkpoint_cutoff = observed_at - AGENT_ASSEMBLY_CHECKPOINT_RETENTION
        durable_cutoff = observed_at - AGENT_FINAL_TIMELINE_RETENTION
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentMessage).where(
                        or_(
                            and_(
                                AgentMessage.is_final.is_(False),
                                AgentMessage.created_at <= checkpoint_cutoff,
                            ),
                            and_(
                                AgentMessage.is_final.is_(True),
                                AgentMessage.created_at <= durable_cutoff,
                            ),
                        )
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class AgentEventRepository:
    """Canonical product events with a B-assigned monotonic conversation cursor
    (plan §4.4).

    ``database_seq`` is assigned by an atomic ``INSERT ... SELECT`` statement:
    the next sequence is computed inside the insert against the conversation's
    existing maximum, so it is database-assigned, monotonic per conversation,
    and safe under concurrent appends.

    Deduplication is part of the same single statement: the insert is guarded
    by ``WHERE NOT EXISTS`` on ``(conversation_id, dedup_key)``.  Because the
    statement reads and writes in one atomic step and SQLite serializes
    writers on the database write lock, concurrent appends of the same
    ``dedup_key`` for a conversation insert exactly one row and every caller
    receives the original event.  (There is no unique constraint on
    ``(conversation_id, dedup_key)`` in the schema; the atomic insert is the
    DB backstop.)
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(
        self,
        *,
        conversation_id: UUID,
        event_kind: str,
        dedup_key: str,
        payload_digest: str,
        run_id: UUID | None = None,
        ephemeral: bool = False,
        payload_json: str | None = None,
    ) -> AgentEvent:
        event, _inserted = await self.append_checked(
            conversation_id=conversation_id,
            event_kind=event_kind,
            dedup_key=dedup_key,
            payload_digest=payload_digest,
            run_id=run_id,
            ephemeral=ephemeral,
            payload_json=payload_json,
        )
        return event

    async def append_checked(
        self,
        *,
        conversation_id: UUID,
        event_kind: str,
        dedup_key: str,
        payload_digest: str,
        run_id: UUID | None = None,
        ephemeral: bool = False,
        payload_json: str | None = None,
    ) -> tuple[AgentEvent, bool]:
        """``append`` plus the dedup verdict (True when the row was inserted).

        Callers that fan out or assemble from the append (the live stream
        hub and the pipeline's message assembly) gate their work on the
        flag so a redelivered event is persisted-at-most-once but never
        re-published or re-assembled.
        """
        if payload_json is not None:
            payload_bytes = payload_json.encode("utf-8")
            if len(payload_bytes) > MAX_AGENT_EVENT_PAYLOAD_BYTES:
                raise ValueError(
                    f"event payload must not exceed {MAX_AGENT_EVENT_PAYLOAD_BYTES} bytes (64 KiB)"
                )
            # Digest invariant (M6a spec §4.2): storage and hash are the same
            # byte string, so the digest stays verifiable and payload/digest
            # drift fails fast instead of corrupting the projection silently.
            if hashlib.sha256(payload_bytes).hexdigest() != payload_digest:
                raise ValueError(
                    "payload_digest must equal sha256(payload_json) when a payload is provided"
                )
        observed_at = datetime.now(UTC)
        next_seq = (
            select(func.coalesce(func.max(AgentEvent.database_seq), 0) + 1)
            .where(AgentEvent.conversation_id == conversation_id)
            .scalar_subquery()
        )
        async with self._sessions() as session:
            result = await session.execute(
                insert(AgentEvent)
                .from_select(
                    [
                        AgentEvent.conversation_id,
                        AgentEvent.run_id,
                        AgentEvent.event_kind,
                        AgentEvent.dedup_key,
                        AgentEvent.database_seq,
                        AgentEvent.payload_digest,
                        AgentEvent.payload,
                        AgentEvent.ephemeral,
                        AgentEvent.created_at,
                    ],
                    select(
                        literal(conversation_id),
                        literal(run_id),
                        literal(event_kind),
                        literal(dedup_key),
                        next_seq,
                        literal(payload_digest),
                        literal(payload_json),
                        literal(ephemeral),
                        literal(observed_at),
                    ).where(
                        ~exists(
                            select(AgentEvent.id).where(
                                AgentEvent.conversation_id == conversation_id,
                                AgentEvent.dedup_key == dedup_key,
                            )
                        )
                    ),
                )
                .returning(AgentEvent)
            )
            event = result.scalar_one_or_none()
            if event is None:
                # Dedup hit: the same (conversation, dedup_key) was already
                # persisted, so return the original event.
                existing = await session.scalar(
                    select(AgentEvent).where(
                        AgentEvent.conversation_id == conversation_id,
                        AgentEvent.dedup_key == dedup_key,
                    )
                )
                # The atomic insert observed the row it skipped, so it must
                # still be visible in this transaction.
                assert existing is not None
                return existing, False
            await session.commit()
            return event, True

    async def list_since_cursor(
        self,
        conversation_id: UUID,
        cursor: int,
        *,
        limit: int | None = None,
    ) -> list[AgentEvent]:
        stmt = (
            select(AgentEvent)
            .where(
                AgentEvent.conversation_id == conversation_id,
                AgentEvent.database_seq > cursor,
            )
            .order_by(AgentEvent.database_seq)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sessions() as session:
            rows = await session.scalars(stmt)
            return list(rows)

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentEvent]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.conversation_id == conversation_id)
                .order_by(AgentEvent.database_seq)
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete events past their §16.1 retention ceiling.

        Ephemeral events (high-frequency ``MESSAGE_DELTA`` streaming deltas)
        follow the 24-hour ``ASSEMBLY_CHECKPOINTS`` ceiling; canonical
        durable-timeline events follow the 30-day ``FINAL_MESSAGES`` ceiling.
        Both ceilings are wired from ``RETENTION_MATRIX`` via
        :data:`AGENT_ASSEMBLY_CHECKPOINT_RETENTION` and
        :data:`AGENT_FINAL_TIMELINE_RETENTION` (plan §15 startup purge sweep,
        §16.1 retention defaults), so the sweep bounds otherwise unbounded
        delta growth.
        """
        observed_at = now or datetime.now(UTC)
        ephemeral_cutoff = observed_at - AGENT_ASSEMBLY_CHECKPOINT_RETENTION
        durable_cutoff = observed_at - AGENT_FINAL_TIMELINE_RETENTION
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentEvent).where(
                        or_(
                            and_(
                                AgentEvent.ephemeral.is_(True),
                                AgentEvent.created_at <= ephemeral_cutoff,
                            ),
                            and_(
                                AgentEvent.ephemeral.is_(False),
                                AgentEvent.created_at <= durable_cutoff,
                            ),
                        )
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class AgentTokenRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        token_hash: str,
        scopes: tuple[str, ...],
        expiry_epoch: int,
        binding_epoch: int,
    ) -> AgentToken:
        async with self._sessions() as session:
            token = AgentToken(
                binding_id=binding_id,
                token_hash=token_hash,
                scopes=_encode_scopes(scopes),
                expiry_epoch=expiry_epoch,
                binding_epoch=binding_epoch,
            )
            session.add(token)
            await session.commit()
            return token

    async def get_by_hash(self, token_hash: str) -> AgentToken | None:
        """Look up a token that is neither revoked nor expired.

        Fail closed: a revoked token must never authenticate again, so the
        lookup excludes rows that carry a ``revoked_at`` stamp.
        """
        async with self._sessions() as session:
            token: AgentToken | None = await session.scalar(
                select(AgentToken).where(
                    AgentToken.token_hash == token_hash,
                    AgentToken.revoked_at.is_(None),
                )
            )
            return token

    async def list_for_binding(self, binding_id: UUID) -> list[AgentToken]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentToken)
                .where(AgentToken.binding_id == binding_id)
                .order_by(AgentToken.created_at)
            )
            return list(rows)

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete tokens whose expiry epoch has passed (plan §15 purge sweep)."""
        observed_at = now or datetime.now(UTC)
        cutoff_epoch = int(observed_at.timestamp())
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentToken).where(AgentToken.expiry_epoch <= cutoff_epoch)
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count

    async def revoke(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentToken)
                .where(
                    AgentToken.token_hash == token_hash,
                    AgentToken.revoked_at.is_(None),
                )
                .values(revoked_at=observed_at)
                .returning(AgentToken.id)
            )
            revoked = result.scalar_one_or_none() is not None
            await session.commit()
            return revoked

    async def expire_all_for_binding(
        self,
        binding_id: UUID,
        *,
        now: datetime | None = None,
    ) -> int:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(AgentToken)
                    .where(
                        AgentToken.binding_id == binding_id,
                        AgentToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=observed_at)
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class PanePolicyRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def set_policy(
        self,
        *,
        binding_id: UUID,
        pane_id: str,
        allowed: bool,
    ) -> PanePolicy:
        async with self._sessions() as session:
            policy = await session.scalar(
                select(PanePolicy).where(
                    PanePolicy.binding_id == binding_id,
                    PanePolicy.pane_id == pane_id,
                )
            )
            if policy is None:
                policy = PanePolicy(
                    binding_id=binding_id,
                    pane_id=pane_id,
                    allowed=allowed,
                )
                session.add(policy)
            else:
                policy.allowed = allowed
            await session.commit()
            return policy

    async def get_for_binding(self, binding_id: UUID) -> list[PanePolicy]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(PanePolicy)
                .where(PanePolicy.binding_id == binding_id)
                .order_by(PanePolicy.pane_id)
            )
            return list(rows)

    async def pane_allowed(self, binding_id: UUID, pane_id: str) -> bool | None:
        async with self._sessions() as session:
            allowed = await session.scalar(
                select(PanePolicy.allowed).where(
                    PanePolicy.binding_id == binding_id,
                    PanePolicy.pane_id == pane_id,
                )
            )
            return allowed


class ApprovalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        conversation_id: UUID,
        tool_call_id: str,
        canonical_hash: str,
        auth_epoch: int,
        expires_at: datetime,
        run_id: UUID | None = None,
        state: str = "pending",
        pane_id: str | None = None,
        operation: str | None = None,
        intent_summary: str | None = None,
    ) -> ApprovalRequest:
        async with self._sessions() as session:
            approval = ApprovalRequest(
                binding_id=binding_id,
                conversation_id=conversation_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                canonical_hash=canonical_hash,
                state=state,
                expires_at=expires_at,
                auth_epoch=auth_epoch,
                pane_id=pane_id,
                operation=operation,
                intent_summary=intent_summary,
            )
            session.add(approval)
            await session.commit()
            return approval

    async def get_by_id(self, approval_id: UUID) -> ApprovalRequest | None:
        async with self._sessions() as session:
            return await session.get(ApprovalRequest, approval_id)

    async def get_by_tool_call(
        self, conversation_id: UUID, tool_call_id: str
    ) -> ApprovalRequest | None:
        async with self._sessions() as session:
            approval: ApprovalRequest | None = await session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.conversation_id == conversation_id,
                    ApprovalRequest.tool_call_id == tool_call_id,
                )
            )
            return approval

    async def set_state(
        self,
        approval_id: UUID,
        new_state: str,
        *,
        expected_state: str = "pending",
        decision: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            if new_state == expected_state:
                # A no-op transition must not stamp decided_at on a decision.
                return await session.get(ApprovalRequest, approval_id)
            result = await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.state == expected_state,
                )
                .values(
                    state=new_state,
                    decided_at=observed_at,
                    decision=decision,
                )
                .returning(ApprovalRequest)
            )
            approval = result.scalar_one_or_none()
            await session.commit()
            return approval

    async def expire_pending(self, *, now: datetime | None = None) -> list[ApprovalRequest]:
        """Sweep pending AND approved requests whose expiry passed (M5.2).

        Returns the swept rows so the policy can record one ``expired``
        audit event per row; ``approved`` rows past expiry are the
        "decided but never executed" zombies the synchronous tool flow can
        never leave behind, and the sweep finishes them the same way.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.state.in_(("pending", "approved")),
                    ApprovalRequest.expires_at <= observed_at,
                )
                .values(state="expired", decided_at=observed_at, decision="expired")
                .returning(ApprovalRequest)
            )
            rows = list(result.scalars())
            await session.commit()
            return rows


class ApprovalAuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        event_type: str,
        approval_id: UUID,
        binding_id: UUID,
        instance_id: UUID | None,
        runtime_epoch: int | None,
        conversation_id: UUID,
        run_id: UUID | None,
        tool_call_id: str,
        pane_id: str | None,
        operation: str | None,
        input_bytes: int | None,
        canonical_hash: str,
        auth_epoch: int,
        actor: str | None,
        outcome: str | None,
        error_code: str | None,
    ) -> ApprovalAuditEvent:
        async with self._sessions() as session:
            event = ApprovalAuditEvent(
                event_type=event_type,
                approval_id=approval_id,
                binding_id=binding_id,
                instance_id=instance_id,
                runtime_epoch=runtime_epoch,
                conversation_id=conversation_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                pane_id=pane_id,
                operation=operation,
                input_bytes=input_bytes,
                canonical_hash=canonical_hash,
                auth_epoch=auth_epoch,
                actor=actor,
                outcome=outcome,
                error_code=error_code,
            )
            session.add(event)
            await session.commit()
            return event

    async def list_for_approval(self, approval_id: UUID) -> list[ApprovalAuditEvent]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ApprovalAuditEvent)
                .where(ApprovalAuditEvent.approval_id == approval_id)
                .order_by(ApprovalAuditEvent.created_at)
            )
            return list(rows)

    async def purge_expired(
        self, *, now: datetime | None = None, older_than: timedelta = AGENT_APPROVAL_AUDIT_RETENTION
    ) -> int:
        """Delete metadata rows after the 90-day retention window (spec §7)."""
        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - older_than
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(ApprovalAuditEvent).where(ApprovalAuditEvent.created_at <= cutoff)
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class WatchRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        binding_id: UUID,
        conversation_id: UUID,
        pane_id: str,
        condition_kind: str,
        start_cursor: str,
        intent_summary: str,
        watch_generation: int = 0,
        rearm_cursor: str | None = None,
        expiry_at: datetime | None = None,
        one_shot: bool = False,
        state: str = "active",
    ) -> Watch:
        async with self._sessions() as session:
            watch = Watch(
                binding_id=binding_id,
                conversation_id=conversation_id,
                pane_id=pane_id,
                condition_kind=condition_kind,
                start_cursor=start_cursor,
                watch_generation=watch_generation,
                rearm_cursor=rearm_cursor,
                intent_summary=intent_summary,
                expiry_at=expiry_at,
                one_shot=one_shot,
                state=state,
            )
            session.add(watch)
            await session.commit()
            return watch

    async def get_by_id(self, watch_id: UUID) -> Watch | None:
        async with self._sessions() as session:
            return await session.get(Watch, watch_id)

    async def list_for_binding(self, binding_id: UUID) -> list[Watch]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Watch).where(Watch.binding_id == binding_id).order_by(Watch.created_at)
            )
            return list(rows)

    async def list_active(self, *, now: datetime | None = None) -> list[Watch]:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Watch)
                .where(
                    Watch.state == "active",
                    or_(
                        Watch.expiry_at.is_(None),
                        Watch.expiry_at > observed_at,
                    ),
                )
                .order_by(Watch.created_at)
            )
            return list(rows)

    async def advance_generation(
        self,
        watch_id: UUID,
        *,
        rearm_cursor: str | None = None,
    ) -> Watch | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(Watch)
                .where(Watch.id == watch_id)
                .values(
                    watch_generation=Watch.watch_generation + 1,
                    rearm_cursor=rearm_cursor,
                )
                .returning(Watch)
            )
            watch = result.scalar_one_or_none()
            await session.commit()
            return watch

    async def cancel(self, watch_id: UUID) -> Watch | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(Watch)
                .where(Watch.id == watch_id, Watch.state == "active")
                .values(state="cancelled")
                .returning(Watch)
            )
            watch = result.scalar_one_or_none()
            await session.commit()
            return watch

    async def set_expiry(self, watch_id: UUID, expiry_at: datetime) -> Watch | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(Watch)
                .where(Watch.id == watch_id)
                .values(expiry_at=expiry_at)
                .returning(Watch)
            )
            watch = result.scalar_one_or_none()
            await session.commit()
            return watch

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete watches whose deadline has passed (plan §15 purge sweep).

        Delivery receipts cascade away with their watch.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(Watch).where(
                        Watch.expiry_at.is_not(None),
                        Watch.expiry_at <= observed_at,
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class WatchDeliveryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_unique(
        self,
        *,
        watch_id: UUID,
        delivery_key: str,
        trigger_event_id: UUID | None = None,
        inbox_item_id: UUID | None = None,
    ) -> WatchDelivery:
        """Create a delivery receipt exactly once per delivery key; a duplicate
        key returns the already-persisted receipt."""
        try:
            async with self._sessions() as session:
                delivery = WatchDelivery(
                    watch_id=watch_id,
                    delivery_key=delivery_key,
                    trigger_event_id=trigger_event_id,
                    inbox_item_id=inbox_item_id,
                )
                session.add(delivery)
                await session.commit()
                return delivery
        except IntegrityError:
            existing = await self.get_by_key(delivery_key)
            if existing is None:
                # A uniqueness race normally means another transaction won
                # and its row is immediately readable.  Treat a missing row
                # as an invariant violation instead of returning ``None``
                # through a repository method whose contract is non-optional.
                raise RuntimeError(
                    "watch delivery uniqueness conflict but the existing row was not found"
                ) from None
            return existing

    async def get_by_key(self, delivery_key: str) -> WatchDelivery | None:
        async with self._sessions() as session:
            delivery: WatchDelivery | None = await session.scalar(
                select(WatchDelivery).where(WatchDelivery.delivery_key == delivery_key)
            )
            return delivery

    async def record_attempt(
        self,
        delivery_id: UUID,
        *,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> WatchDelivery | None:
        async with self._sessions() as session:
            result = await session.execute(
                update(WatchDelivery)
                .where(WatchDelivery.id == delivery_id)
                .values(
                    attempt_count=WatchDelivery.attempt_count + 1,
                    last_error=last_error,
                    next_attempt_at=next_attempt_at,
                )
                .returning(WatchDelivery)
            )
            delivery = result.scalar_one_or_none()
            await session.commit()
            return delivery


class CleanupJobRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _receipt_values(
        *,
        artifact_kind: str,
        artifact_ref: str,
        state: str = "pending",
        policy_reason: str | None = None,
        policy_version: str | None = None,
        evidence_digest: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        observed = now or datetime.now(UTC)
        return {
            "artifact_kind": artifact_kind,
            "artifact_ref": artifact_ref,
            "state": state,
            "attempt_count": 0,
            "last_error": None,
            "next_attempt_at": None,
            "evidence_digest": evidence_digest,
            "policy_reason": policy_reason,
            "policy_version": policy_version,
            "confirmed_at": observed if state in {"confirmed", "not_applicable"} else None,
            "created_at": observed,
            "updated_at": observed,
        }

    async def _claim_job(
        self,
        session: AsyncSession,
        *,
        target_kind: str,
        target_ref: str,
        term_id: UUID | None,
        installation_id: UUID | None,
        state: str,
        manifest_version: int,
        next_attempt_at: datetime | None,
    ) -> tuple[AgentCleanupJob, bool]:
        observed = datetime.now(UTC)
        values: dict[str, object] = {
            "term_id": term_id,
            "installation_id": installation_id,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "state": state,
            "manifest_version": manifest_version,
            "attempt_count": 0,
            "last_error": None,
            "next_attempt_at": next_attempt_at,
            "completed_at": observed if state == "completed" else None,
            "created_at": observed,
            "updated_at": observed,
        }
        bind = session.get_bind()
        dialect = bind.dialect.name
        inserted_id: UUID | None = None
        statement: Any
        if dialect == "sqlite":
            statement = sqlite_insert(AgentCleanupJob).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[AgentCleanupJob.target_kind, AgentCleanupJob.target_ref]
            ).returning(AgentCleanupJob.id)
            inserted_id = cast(UUID | None, (await session.execute(statement)).scalar_one_or_none())
        elif dialect == "postgresql":
            statement = postgresql_insert(AgentCleanupJob).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[AgentCleanupJob.target_kind, AgentCleanupJob.target_ref]
            ).returning(AgentCleanupJob.id)
            inserted_id = cast(UUID | None, (await session.execute(statement)).scalar_one_or_none())
        else:
            try:
                candidate = AgentCleanupJob(**values)
                session.add(candidate)
                await session.flush()
                inserted_id = candidate.id
            except IntegrityError:
                await session.rollback()

        if inserted_id is not None:
            job = await session.get(AgentCleanupJob, inserted_id)
            if job is None:
                raise RuntimeError("cleanup manifest insert returned no row")
            return job, True
        job = await session.scalar(
            select(AgentCleanupJob).where(
                AgentCleanupJob.target_kind == target_kind,
                AgentCleanupJob.target_ref == target_ref,
            )
        )
        if job is None:
            raise RuntimeError("cleanup manifest claim lost without an existing row")
        return job, False

    async def create_manifest(
        self,
        *,
        target_kind: str,
        target_ref: str,
        receipts: list[dict[str, object]],
        term_id: UUID | None = None,
        installation_id: UUID | None = None,
        manifest_version: int = 1,
        state: str = "pending",
        next_attempt_at: datetime | None = None,
    ) -> tuple[AgentCleanupJob, bool]:
        """Atomically create or reuse a target's cleanup manifest."""

        async with self._sessions() as session:
            job, owner = await self._claim_job(
                session,
                target_kind=target_kind,
                target_ref=target_ref,
                term_id=term_id,
                installation_id=installation_id,
                state=state,
                manifest_version=manifest_version,
                next_attempt_at=next_attempt_at,
            )
            if owner:
                for values in receipts:
                    session.add(AgentCleanupReceipt(cleanup_job_id=job.id, **values))
                await session.flush()
            await session.commit()
            return job, owner

    async def create(
        self,
        *,
        target_kind: str,
        target_ref: str,
        term_id: UUID | None = None,
        installation_id: UUID | None = None,
        state: str = "pending",
        next_attempt_at: datetime | None = None,
    ) -> AgentCleanupJob:
        # Keep the legacy call shape while ensuring every new job has at
        # least one auditable manifest entry.
        receipt = self._receipt_values(
            artifact_kind=target_kind,
            artifact_ref=target_ref,
            state="confirmed" if state == "completed" else "pending",
            evidence_digest=(
                digest_secret(f"legacy:{target_kind}:{target_ref}")
                if state == "completed"
                else None
            ),
        )
        job, _ = await self.create_manifest(
            target_kind=target_kind,
            target_ref=target_ref,
            receipts=[receipt],
            term_id=term_id,
            installation_id=installation_id,
            state=state,
            next_attempt_at=next_attempt_at,
        )
        return job

    async def get(self, job_id: UUID) -> AgentCleanupJob | None:
        async with self._sessions() as session:
            return await session.get(AgentCleanupJob, job_id)

    async def get_by_target(
        self,
        *,
        target_kind: str,
        target_ref: str,
    ) -> AgentCleanupJob | None:
        async with self._sessions() as session:
            return cast(
                AgentCleanupJob | None,
                await session.scalar(
                    select(AgentCleanupJob).where(
                        AgentCleanupJob.target_kind == target_kind,
                        AgentCleanupJob.target_ref == target_ref,
                    )
                ),
            )

    async def list_receipts(self, job_id: UUID) -> list[AgentCleanupReceipt]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentCleanupReceipt)
                .where(AgentCleanupReceipt.cleanup_job_id == job_id)
                .order_by(AgentCleanupReceipt.created_at, AgentCleanupReceipt.id)
            )
            return list(rows)

    async def _refresh_job_state(
        self,
        session: AsyncSession,
        job_id: UUID,
        *,
        now: datetime,
    ) -> AgentCleanupJob | None:
        job = await session.get(AgentCleanupJob, job_id)
        if job is None:
            return None
        receipts = list(
            await session.scalars(
                select(AgentCleanupReceipt).where(AgentCleanupReceipt.cleanup_job_id == job_id)
            )
        )
        if not receipts:
            # Empty manifests are never successful.  This also repairs a
            # pre-0012 row that was incorrectly marked completed.
            if job.state == "completed":
                job.state = "pending"
                job.completed_at = None
            return job
        if any(receipt.state == "dead_letter" for receipt in receipts):
            job.state = "dead_letter"
            job.completed_at = None
        elif all(
            receipt.state == "confirmed"
            or (
                receipt.state == "not_applicable"
                and receipt.policy_reason
                and receipt.policy_reason.strip()
                and receipt.policy_version
                and receipt.policy_version.strip()
            )
            for receipt in receipts
        ):
            job.state = "completed"
            job.completed_at = job.completed_at or now
        else:
            job.state = "pending"
            job.completed_at = None
        job.updated_at = now
        return job

    async def refresh_state(
        self,
        job_id: UUID,
        *,
        now: datetime | None = None,
    ) -> AgentCleanupJob | None:
        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            job = await self._refresh_job_state(session, job_id, now=observed)
            await session.commit()
            return job

    async def mark_receipt_attempt(
        self,
        receipt_id: UUID,
        *,
        next_attempt_at: datetime,
        last_error: str | None = None,
        now: datetime | None = None,
    ) -> AgentCleanupReceipt | None:
        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentCleanupReceipt)
                .where(
                    AgentCleanupReceipt.id == receipt_id,
                    AgentCleanupReceipt.state == "pending",
                )
                .values(
                    attempt_count=AgentCleanupReceipt.attempt_count + 1,
                    next_attempt_at=next_attempt_at,
                    last_error=last_error,
                    updated_at=observed,
                )
                .returning(AgentCleanupReceipt)
            )
            receipt = result.scalar_one_or_none()
            if receipt is not None:
                await self._refresh_job_state(session, receipt.cleanup_job_id, now=observed)
            await session.commit()
            return receipt

    async def mark_receipt_confirmed(
        self,
        receipt_id: UUID,
        *,
        evidence_digest: str,
        now: datetime | None = None,
    ) -> AgentCleanupReceipt | None:
        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            receipt = await session.get(AgentCleanupReceipt, receipt_id)
            if receipt is None:
                return None
            if receipt.state == "confirmed":
                if receipt.evidence_digest != evidence_digest:
                    raise ValueError("receipt evidence conflicts with prior confirmation")
                return receipt
            if receipt.state != "pending":
                raise ValueError("receipt is not confirmable")
            receipt.state = "confirmed"
            receipt.evidence_digest = evidence_digest
            receipt.confirmed_at = observed
            receipt.next_attempt_at = None
            receipt.last_error = None
            receipt.updated_at = observed
            await self._refresh_job_state(session, receipt.cleanup_job_id, now=observed)
            await session.commit()
            return receipt

    async def mark_receipt_not_applicable(
        self,
        receipt_id: UUID,
        *,
        policy_reason: str,
        policy_version: str,
        now: datetime | None = None,
    ) -> AgentCleanupReceipt | None:
        if not policy_reason.strip() or not policy_version.strip():
            raise ValueError("not_applicable receipts require policy reason and version")
        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            receipt = await session.get(AgentCleanupReceipt, receipt_id)
            if receipt is None:
                return None
            if receipt.state == "not_applicable":
                if (
                    receipt.policy_reason != policy_reason
                    or receipt.policy_version != policy_version
                ):
                    raise ValueError("receipt policy conflicts with prior decision")
                return receipt
            if receipt.state != "pending":
                raise ValueError("receipt is not transitionable")
            receipt.state = "not_applicable"
            receipt.policy_reason = policy_reason
            receipt.policy_version = policy_version
            receipt.confirmed_at = observed
            receipt.next_attempt_at = None
            receipt.last_error = None
            receipt.updated_at = observed
            await self._refresh_job_state(session, receipt.cleanup_job_id, now=observed)
            await session.commit()
            return receipt

    async def mark_receipt_dead_letter(
        self,
        receipt_id: UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> AgentCleanupReceipt | None:
        if not reason.strip():
            raise ValueError("dead-letter reason must not be empty")
        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            receipt = await session.get(AgentCleanupReceipt, receipt_id)
            if receipt is None:
                return None
            if receipt.state == "dead_letter":
                return receipt
            if receipt.state in {"confirmed", "not_applicable"}:
                raise ValueError("terminal receipt cannot become dead-letter")
            receipt.state = "dead_letter"
            receipt.last_error = reason[:1024]
            receipt.policy_reason = reason[:128]
            receipt.next_attempt_at = None
            receipt.updated_at = observed
            await self._refresh_job_state(session, receipt.cleanup_job_id, now=observed)
            await session.commit()
            return receipt

    async def confirm_pending_internal(
        self,
        job_id: UUID,
        *,
        evidence_digest: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Confirm all pending receipts after a B-owned cleanup handler."""

        observed = now or datetime.now(UTC)
        async with self._sessions() as session:
            receipts = list(
                await session.scalars(
                    select(AgentCleanupReceipt).where(
                        AgentCleanupReceipt.cleanup_job_id == job_id,
                        AgentCleanupReceipt.state == "pending",
                        AgentCleanupReceipt.artifact_kind.in_(
                            [
                                "conversation",
                                "binding",
                                "profile",
                                "term",
                                "installation",
                                "b_row",
                                "watch",
                                "approval",
                                "agent_token",
                                "pane_policy",
                                "agent_event",
                                "agent_message",
                                "agent_inbox",
                                "agent_run",
                                "agent_tool_request",
                                "backend_session",
                            ]
                        ),
                    )
                )
            )
            for receipt in receipts:
                receipt.state = "confirmed"
                receipt.evidence_digest = evidence_digest or digest_secret(
                    f"cleanup:{job_id}:{receipt.artifact_kind}:{receipt.artifact_ref}"
                )
                receipt.confirmed_at = observed
                receipt.next_attempt_at = None
                receipt.last_error = None
                receipt.updated_at = observed
            await self._refresh_job_state(session, job_id, now=observed)
            await session.commit()
            return len(receipts)

    async def confirm_helper(
        self,
        *,
        job_id: UUID,
        receipt_id: UUID,
        artifact_ref: str,
        result: str,
        evidence_digest: str | None,
        reason_code: str | None,
        idempotency_key: UUID,
    ) -> AgentCleanupReceipt:
        observed = datetime.now(UTC)
        async with self._sessions() as session:
            changed = await session.scalar(
                update(AgentCleanupReceipt)
                .where(
                    AgentCleanupReceipt.id == receipt_id,
                    AgentCleanupReceipt.cleanup_job_id == job_id,
                    AgentCleanupReceipt.artifact_ref == artifact_ref,
                    AgentCleanupReceipt.artifact_kind.in_(
                        [
                            "container",
                            "volume",
                            "log",
                            "provider",
                            "runtime",
                            "runtime_attestation",
                            "runtime_volume",
                            "container_log",
                            "provider_retention",
                            "sqlite_wal_backup",
                        ]
                    ),
                    AgentCleanupReceipt.state == "pending",
                    AgentCleanupReceipt.confirmation_key.is_(None),
                )
                .values(
                    state=result,
                    evidence_digest=evidence_digest,
                    policy_reason=reason_code,
                    confirmation_key=idempotency_key,
                    confirmed_at=observed if result == "confirmed" else None,
                    last_error=reason_code,
                    next_attempt_at=None,
                    updated_at=observed,
                )
                .returning(AgentCleanupReceipt)
            )
            if changed is None:
                existing = await session.get(AgentCleanupReceipt, receipt_id)
                if existing is None or existing.cleanup_job_id != job_id:
                    raise KeyError(receipt_id)
                if (
                    existing.artifact_ref != artifact_ref
                    or existing.state != result
                    or existing.confirmation_key != idempotency_key
                    or existing.evidence_digest != evidence_digest
                    or existing.policy_reason != reason_code
                ):
                    raise ValueError("cleanup confirmation conflict")
                return existing
            await self._refresh_job_state(session, job_id, now=observed)
            await session.commit()
            return changed

    async def list_pending(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[AgentCleanupJob]:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentCleanupJob)
                .where(
                    AgentCleanupJob.state == "pending",
                    or_(
                        AgentCleanupJob.next_attempt_at.is_(None),
                        AgentCleanupJob.next_attempt_at <= observed_at,
                    ),
                )
                .order_by(AgentCleanupJob.created_at)
                .limit(limit)
            )
            return list(rows)

    async def list_pending_retry_now(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[AgentCleanupJob]:
        """Pending jobs due now, plus any pending job whose previous attempt
        failed because no cleanup handler was registered.

        The handlers are registered by the plugin startup; a job that the
        pre-startup recovery swept before registration recorded that error
        and a retry backoff.  Once the handlers exist the backoff no longer
        needs to be honored, so the plugin's first cleanup sweep includes
        those rows even when they are not due yet.
        """
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentCleanupJob)
                .where(
                    AgentCleanupJob.state == "pending",
                    or_(
                        AgentCleanupJob.next_attempt_at.is_(None),
                        AgentCleanupJob.next_attempt_at <= observed_at,
                        AgentCleanupJob.last_error.like("no cleanup handler registered%"),
                    ),
                )
                .order_by(AgentCleanupJob.created_at)
                .limit(limit)
            )
            return list(rows)

    async def record_attempt(
        self,
        job_id: UUID,
        *,
        next_attempt_at: datetime,
        last_error: str | None = None,
    ) -> AgentCleanupJob | None:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentCleanupJob)
                .where(AgentCleanupJob.id == job_id)
                .values(
                    attempt_count=AgentCleanupJob.attempt_count + 1,
                    next_attempt_at=next_attempt_at,
                    last_error=last_error,
                    updated_at=observed_at,
                )
                .returning(AgentCleanupJob)
            )
            job = result.scalar_one_or_none()
            if job is not None:
                await session.execute(
                    update(AgentCleanupReceipt)
                    .where(
                        AgentCleanupReceipt.cleanup_job_id == job_id,
                        AgentCleanupReceipt.state == "pending",
                    )
                    .values(
                        attempt_count=AgentCleanupReceipt.attempt_count + 1,
                        next_attempt_at=next_attempt_at,
                        last_error=last_error,
                        updated_at=observed_at,
                    )
                )
            await session.commit()
            return job

    async def complete(
        self,
        job_id: UUID,
        *,
        now: datetime | None = None,
    ) -> AgentCleanupJob | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            job = await self._refresh_job_state(session, job_id, now=observed_at)
            if job is None or job.state != "completed":
                await session.rollback()
                return None
            await session.commit()
            return job

    async def dead_letter(
        self,
        job_id: UUID,
        *,
        last_error: str | None = None,
        now: datetime | None = None,
    ) -> AgentCleanupJob | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentCleanupJob)
                .where(
                    AgentCleanupJob.id == job_id,
                    AgentCleanupJob.state == "pending",
                )
                .values(
                    state="dead_letter",
                    last_error=last_error,
                    completed_at=None,
                    updated_at=observed_at,
                )
                .returning(AgentCleanupJob)
            )
            job = result.scalar_one_or_none()
            await session.commit()
            return job

    async def purge_completed(
        self,
        *,
        now: datetime | None = None,
        older_than: timedelta = AGENT_TERMINAL_RETENTION,
    ) -> int:
        """Delete confirmed tombstones after the retention window (plan §15,
        §16.1: confirmed cleanup tombstones are retained 30 days).

        Only resolved jobs (``completed`` or ``dead_letter``) are removed;
        pending tombstones stay pending until cleanup is confirmed.
        """
        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - older_than
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentCleanupJob).where(
                        AgentCleanupJob.state.in_(("completed", "dead_letter")),
                        AgentCleanupJob.updated_at <= cutoff,
                    )
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class DiagnosticsRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(
        self,
        *,
        conversation_id: UUID | None,
        event_kind: str,
        correlation_hash: str,
        ttl_expires_at: datetime,
        provider_type: str | None = None,
        provider_session_id: str | None = None,
        provider_run_id: str | None = None,
        error_code: str | None = None,
        payload_metadata: str | None = None,
    ) -> AgentDiagnostic:
        async with self._sessions() as session:
            diagnostic = AgentDiagnostic(
                conversation_id=conversation_id,
                event_kind=event_kind,
                provider_type=provider_type,
                provider_session_id=provider_session_id,
                provider_run_id=provider_run_id,
                error_code=error_code,
                correlation_hash=correlation_hash,
                payload_metadata=payload_metadata,
                ttl_expires_at=ttl_expires_at,
            )
            session.add(diagnostic)
            await session.commit()
            return diagnostic

    async def prune_expired(self, *, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(AgentDiagnostic).where(AgentDiagnostic.ttl_expires_at <= observed_at)
                ),
            )
            count = int(result.rowcount or 0)
            await session.commit()
            return count


class RepositoryBundle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.enrollments = EnrollmentRepository(sessions)
        self.installations = InstallationRepository(sessions)
        self.instances = InstanceRepository(sessions)
        self.audit = AuditRepository(sessions)
        self.auth_state = AuthStateRepository(sessions)
        self.totp_setups = TotpSetupRepository(sessions)
        self.auth_challenges = AuthChallengeRepository(sessions)
        self.native_clients = NativeClientRepository(sessions)
        self.oauth_authorizations = OAuthAuthorizationRepository(sessions)
        self.auth_tokens = AuthTokenRepository(sessions)
        self.auth_audit = AuthAuditRepository(sessions)
        # Agent Broker domain repositories (plan §15).
        self.agent_profiles = AgentProfileRepository(sessions)
        self.agent_bindings = AgentBindingRepository(sessions)
        self.agent_setup_receipts = AgentSetupReceiptRepository(sessions)
        self.agent_memory_scopes = AgentMemoryScopeRepository(sessions)
        self.agent_conversations = AgentConversationRepository(sessions)
        self.agent_backend_conversations = AgentBackendConversationRepository(sessions)
        self.agent_provider_disclosures = AgentProviderDisclosureAcceptanceRepository(sessions)
        self.agent_runtime_bindings = AgentRuntimeBindingRepository(sessions)
        self.agent_inbox = AgentInboxRepository(sessions)
        self.agent_tool_requests = AgentToolRequestRepository(sessions)
        self.agent_runs = AgentRunRepository(sessions)
        self.agent_messages = AgentMessageRepository(sessions)
        self.agent_events = AgentEventRepository(sessions)
        self.agent_tokens = AgentTokenRepository(sessions)
        self.pane_policies = PanePolicyRepository(sessions)
        self.approvals = ApprovalRepository(sessions)
        self.approval_audit = ApprovalAuditRepository(sessions)
        self.watches = WatchRepository(sessions)
        self.agent_watch_deliveries = WatchDeliveryRepository(sessions)
        self.cleanup_jobs = CleanupJobRepository(sessions)
        self.diagnostics = DiagnosticsRepository(sessions)

    async def purge_core_expired(self, *, now: datetime) -> dict[str, int]:
        """Purge only core-owned expiring rows.

        This is used during a degraded startup when the Agent migration stage
        could not be established.  It deliberately never touches an Agent
        table, allowing authentication and terminal APIs to remain available
        while the Agent stage is retried on the next startup.
        """

        counts: dict[str, int] = {}
        async with self._sessions() as session:
            for name, model in (
                ("enrollment_tokens", EnrollmentToken),
                ("auth_tokens", AuthToken),
                ("totp_setups", TotpSetup),
                ("auth_challenges", AuthChallenge),
                ("oauth_authorizations", OAuthAuthorization),
            ):
                result = cast(
                    CursorResult[Any],
                    await session.execute(delete(model).where(model.expires_at < now)),
                )
                counts[name] = int(result.rowcount or 0)
            await session.commit()
        return counts

    async def purge_expired(self, *, now: datetime) -> dict[str, int]:
        """Delete rows past their expiry; native clients are retained forever.

        The same startup sweep covers the Agent Broker classes (plan §15):
        expired tokens are deleted, diagnostics are pruned, expired watches
        are deleted, canonical events and message checkpoints past their
        §16.1 retention ceilings are removed (ephemeral MESSAGE_DELTA deltas
        after 24 hours, final timeline rows after 30 days), and inbox/run
        rows and cleanup tombstones in terminal states older than the
        retention window are removed.  Expired approvals are NOT swept here:
        the composition root sweeps them through ``ApprovalPolicy`` so every
        swept row records an ``expired`` audit event (spec §5/§7).
        """

        counts = await self.purge_core_expired(now=now)
        counts["agent_tokens"] = await self.agent_tokens.purge_expired(now=now)
        counts["approval_audit"] = await self.approval_audit.purge_expired(now=now)
        counts["diagnostics"] = await self.diagnostics.prune_expired(now=now)
        counts["watches"] = await self.watches.purge_expired(now=now)
        counts["agent_inbox"] = await self.agent_inbox.purge_terminal(now=now)
        counts["agent_runs"] = await self.agent_runs.purge_terminal(now=now)
        counts["agent_events"] = await self.agent_events.purge_expired(now=now)
        counts["agent_messages"] = await self.agent_messages.purge_expired(now=now)
        counts["cleanup_jobs"] = await self.cleanup_jobs.purge_completed(now=now)
        return counts
