"""Small transactional repositories for Control Plane metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.auth.secret_box import EncryptedSecret

from .models import (
    AuditEvent,
    AuthAuditEvent,
    AuthChallenge,
    AuthenticationState,
    AuthToken,
    EnrollmentToken,
    Installation,
    Instance,
    NativeClient,
    OAuthAuthorization,
    TotpSetup,
)


def digest_secret(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


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
    now: datetime | None = None,
) -> AuthToken | None:
    observed_at = now or datetime.now(UTC)
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
                AuthToken.created_at,
            ],
            source,
        )
        .returning(AuthToken)
    )
    token = result.scalar_one_or_none()
    if token is not None:
        await session.execute(
            update(NativeClient)
            .where(NativeClient.id == client_id, NativeClient.revoked_at.is_(None))
            .values(last_used_at=observed_at, updated_at=observed_at)
        )
    return token


class InstanceOwnershipError(RuntimeError):
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
            result = await session.scalars(select(Installation).order_by(Installation.created_at))
            return list(result)

    async def rename(self, installation_id: UUID, display_name: str) -> Installation | None:
        async with self._sessions() as session:
            installation = await session.get(Installation, installation_id)
            if installation is None:
                return None
            installation.display_name = display_name
            await session.commit()
            return installation


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
                    source_digest=audit_source_digest
                    or digest_secret("local-control-plane"),
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
        """Return the stable JKT owner, including across concurrent first authorization."""

        existing = await self.get_by_thumbprint(key_thumbprint)
        if existing is not None:
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


class OAuthAuthorizationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

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
                    OAuthAuthorization.transaction_digest
                    == digest_secret(transaction_secret),
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
                    OAuthAuthorization.authorization_code_digest
                    == digest_secret(raw_code),
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


class RepositoryBundle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
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
