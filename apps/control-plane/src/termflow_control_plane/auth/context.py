"""Authentication context shared by administrator dependencies.

The context deliberately carries only an opaque actor reference and the time
of the original strong authentication.  Raw credentials never belong in this
object (or in an error generated from it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    SQLite returns values from ``DateTime(timezone=True)`` as naive datetimes.
    Treating those values as UTC at the boundary keeps freshness comparisons
    deterministic without rewriting the original persisted timestamp.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AdminAuthContext:
    """The authenticated administrator principal and freshness evidence."""

    credential_kind: str
    actor_ref: str
    auth_epoch: int
    authenticated_at: datetime | None

    def is_fresh(self, *, now: datetime, maximum_age: timedelta) -> bool:
        """Whether the original strong authentication is within ``maximum_age``.

        A missing timestamp can never prove freshness.  Clock rollback is
        treated as stale rather than allowing a future-dated credential to
        perform a sensitive action.
        """

        if self.authenticated_at is None or maximum_age < timedelta(0):
            return False
        age = as_utc(now) - as_utc(self.authenticated_at)
        return timedelta(0) <= age <= maximum_age


__all__ = ["AdminAuthContext", "as_utc"]
