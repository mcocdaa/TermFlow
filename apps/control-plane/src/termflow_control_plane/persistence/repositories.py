"""Small transactional repositories for Control Plane metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, or_, select, update
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


class InstanceOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConsumedEnrollment:
    id: UUID
    display_name: str | None


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

    async def enable_totp(
        self,
        encrypted: EncryptedSecret,
        counter: int,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            statement = update(AuthenticationState).where(AuthenticationState.id == 1)
            if expected_epoch is not None:
                statement = statement.where(AuthenticationState.epoch == expected_epoch)
            result = await session.execute(
                statement.values(
                    totp_ciphertext=encrypted.ciphertext,
                    totp_nonce=encrypted.nonce,
                    totp_key_version=encrypted.key_version,
                    totp_aad_version=encrypted.aad_version,
                    totp_enabled_at=observed_at,
                    totp_last_accepted_counter=counter,
                    updated_at=observed_at,
                )
                .returning(AuthenticationState.id)
            )
            updated = result.scalar_one_or_none()
            if updated is None and expected_epoch is None:
                raise RuntimeError("authentication state singleton is missing")
            await session.commit()
            return updated is not None

    async def accept_totp_counter(self, counter: int) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(AuthenticationState)
                .where(
                    AuthenticationState.id == 1,
                    AuthenticationState.totp_enabled_at.is_not(None),
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

    async def reset_and_increment_epoch(self) -> int:
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

    async def fail_attempt(self, challenge_id: UUID, maximum: int = 5) -> bool:
        observed_at = datetime.now(UTC)
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
    ) -> NativeClient:
        async with self._sessions() as session:
            client = NativeClient(
                id=client_id or uuid4(),
                display_name=display_name,
                public_jwk=public_jwk,
                key_thumbprint=key_thumbprint,
                platform=platform,
                scopes=_encode_scopes(scopes),
            )
            session.add(client)
            await session.commit()
            return client

    async def get(self, client_id: UUID) -> NativeClient | None:
        async with self._sessions() as session:
            return await session.get(NativeClient, client_id)

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
    ) -> UUID:
        async with self._sessions() as session:
            authorization = OAuthAuthorization(
                transaction_digest=digest_secret(transaction_secret),
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=_encode_scopes(scopes),
                pkce_challenge=pkce_challenge,
                expires_at=expires_at,
                epoch=epoch,
            )
            session.add(authorization)
            await session.commit()
            return authorization.id

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
                )
            )
            return authorization

    async def issue_code(self, authorization_id: UUID, raw_code: str) -> bool:
        observed_at = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.id == authorization_id,
                    OAuthAuthorization.authorization_code_digest.is_(None),
                    OAuthAuthorization.consumed_at.is_(None),
                    OAuthAuthorization.expires_at > observed_at,
                )
                .values(
                    authorization_code_digest=digest_secret(raw_code),
                    code_issued_at=observed_at,
                    approved_at=func.coalesce(OAuthAuthorization.approved_at, observed_at),
                )
                .returning(OAuthAuthorization.id)
            )
            issued = result.scalar_one_or_none() is not None
            await session.commit()
            return issued

    async def consume_code(
        self,
        raw_code: str,
        *,
        epoch: int,
        now: datetime | None = None,
    ) -> OAuthAuthorization | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                update(OAuthAuthorization)
                .where(
                    OAuthAuthorization.authorization_code_digest == digest_secret(raw_code),
                    OAuthAuthorization.epoch == epoch,
                    OAuthAuthorization.expires_at > observed_at,
                    OAuthAuthorization.consumed_at.is_(None),
                )
                .values(consumed_at=observed_at)
                .returning(OAuthAuthorization)
            )
            authorization = result.scalar_one_or_none()
            await session.commit()
            return authorization


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
            token = AuthToken(
                token_digest=digest_secret(raw_token),
                kind=kind,
                scopes=_encode_scopes(scopes),
                key_thumbprint=key_thumbprint,
                expires_at=expires_at,
                epoch=epoch,
                client_id=client_id,
                family_id=family_id or (uuid4() if kind == "refresh" else None),
                parent_token_id=parent_token_id,
            )
            session.add(token)
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
        ]
        if kind is not None:
            conditions.append(AuthToken.kind == kind)
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
                await session.commit()
                return None
            token = AuthToken(
                token_digest=digest_secret(replacement),
                kind="refresh",
                client_id=row[1],
                scopes=row[2],
                key_thumbprint=row[3],
                family_id=row[4],
                parent_token_id=row[0],
                epoch=epoch,
                expires_at=expires_at,
            )
            session.add(token)
            await session.commit()
            return token

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
