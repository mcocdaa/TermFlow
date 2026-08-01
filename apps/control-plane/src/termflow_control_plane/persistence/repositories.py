"""Small transactional repositories for Control Plane metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import AuditEvent, EnrollmentToken, Installation, Instance


class InstanceOwnershipError(RuntimeError):
    pass


class EnrollmentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, token_hash: str, expires_at: datetime) -> EnrollmentToken:
        async with self._sessions() as session:
            enrollment = EnrollmentToken(token_hash=token_hash, expires_at=expires_at)
            session.add(enrollment)
            await session.commit()
            return enrollment

    async def consume(self, token_hash: str, *, now: datetime | None = None) -> UUID | None:
        observed_at = now or datetime.now(UTC)
        async with self._sessions() as session:
            enrollment_id = await session.scalar(
                update(EnrollmentToken)
                .where(
                    EnrollmentToken.token_hash == token_hash,
                    EnrollmentToken.used_at.is_(None),
                    EnrollmentToken.expires_at > observed_at,
                )
                .values(used_at=observed_at)
                .returning(EnrollmentToken.id)
            )
            await session.commit()
            return enrollment_id


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


class RepositoryBundle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.enrollments = EnrollmentRepository(sessions)
        self.installations = InstallationRepository(sessions)
        self.instances = InstanceRepository(sessions)
        self.audit = AuditRepository(sessions)
