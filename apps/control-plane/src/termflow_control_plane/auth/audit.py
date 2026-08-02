"""Secret-free authentication audit events, separate from terminal statistics."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class AuthAuditOperation(StrEnum):
    WEB_SESSION_LOGIN = "web_session.login"
    NATIVE_AUTHORIZATION = "native.authorization"
    TOKEN_EXCHANGE = "token.exchange"
    TOTP_VERIFICATION = "totp.verify"
    CLI_LOGIN = "cli.login"
    AUTH_RESET = "auth.reset"


class AuthAuditResult(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    RESET = "reset"


class AuthAuditErrorCode(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    ORIGIN_REJECTED = "origin_rejected"
    CHALLENGE_EXPIRED = "challenge_expired"
    RATE_LIMITED = "rate_limited"


class AuthAuditRepository(Protocol):
    async def record(
        self,
        operation: str,
        result: str,
        source_digest: str,
        *,
        client_id: UUID | None = None,
        error_code: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class AuthAuditEvent:
    operation: AuthAuditOperation
    result: AuthAuditResult
    source_digest: str
    occurred_at: datetime
    client_id: UUID | None = None
    error_code: AuthAuditErrorCode | None = None


class AuthenticationAudit:
    """Create allowlisted events while hashing the direct peer before persistence."""

    __slots__ = ("_clock", "_digest_key", "_repository")

    def __init__(
        self,
        repository: AuthAuditRepository | None,
        *,
        digest_key: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._digest_key = digest_key or secrets.token_bytes(32)
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        repository_name = (
            type(self._repository).__name__ if self._repository is not None else "disabled"
        )
        return f"AuthenticationAudit(repository={repository_name})"

    async def record(
        self,
        operation: AuthAuditOperation,
        result: AuthAuditResult,
        source: str,
        *,
        client_id: UUID | None = None,
        error_code: AuthAuditErrorCode | None = None,
    ) -> AuthAuditEvent:
        digest = hmac.new(
            self._digest_key,
            source.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        event = AuthAuditEvent(
            operation=operation,
            result=result,
            source_digest=digest,
            occurred_at=self._clock(),
            client_id=client_id,
            error_code=error_code,
        )
        if self._repository is not None:
            await self._repository.record(
                operation.value,
                result.value,
                digest,
                client_id=client_id,
                error_code=error_code.value if error_code is not None else None,
            )
        return event
