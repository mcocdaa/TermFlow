"""Small transactional repositories for Control Plane metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
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
            enrollment = await session.scalar(
                select(EnrollmentToken).where(
                    EnrollmentToken.token_hash == token_hash,
                    EnrollmentToken.used_at.is_(None),
                    EnrollmentToken.expires_at > observed_at,
                )
            )
            if enrollment is None:
                return None
            enrollment.used_at = observed_at
            await session.commit()
            return enrollment.id


class InstallationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, token_hash: str) -> Installation:
        async with self._sessions() as session:
            installation = Installation(token_hash=token_hash)
            session.add(installation)
            await session.commit()
            return installation

    async def get_by_token_hash(self, token_hash: str) -> Installation | None:
        async with self._sessions() as session:
            installation: Installation | None = await session.scalar(
                select(Installation).where(
                    Installation.token_hash == token_hash,
                    Installation.revoked_at.is_(None),
                )
            )
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
        async with self._sessions() as session:
            instance = await session.get(Instance, instance_id)
            if instance is None:
                instance = Instance(
                    id=instance_id,
                    installation_id=installation_id,
                    name=name,
                    token_hash=token_hash,
                )
                session.add(instance)
            elif instance.installation_id != installation_id:
                raise InstanceOwnershipError(str(instance_id))
            else:
                instance.name = name
                instance.token_hash = token_hash
                instance.revoked_at = None
            await session.commit()
            return instance

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


class RepositoryBundle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.enrollments = EnrollmentRepository(sessions)
        self.installations = InstallationRepository(sessions)
        self.instances = InstanceRepository(sessions)
        self.audit = AuditRepository(sessions)
