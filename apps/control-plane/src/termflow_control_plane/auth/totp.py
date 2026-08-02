"""RFC 6238 TOTP primitives for the fixed TermFlow V1 authenticator profile."""

from __future__ import annotations

import hashlib
import hmac
import struct
from datetime import datetime

_PERIOD_SECONDS = 30
_V1_DIGITS = 6


def totp_for_counter(secret: bytes, counter: int, *, digits: int = _V1_DIGITS) -> str:
    """Return an RFC 4226/6238 HMAC-SHA-1 code for one non-negative counter."""

    if not secret:
        raise ValueError("TOTP secret must not be empty")
    if counter < 0:
        raise ValueError("TOTP counter must not be negative")
    if digits not in {6, 8}:
        raise ValueError("TOTP digits must be six or eight")
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return f"{truncated % (10**digits):0{digits}d}"


def counter_at(observed_at: datetime) -> int:
    """Return the fixed 30-second counter for an aware wall-clock timestamp."""

    if observed_at.tzinfo is None:
        raise ValueError("TOTP timestamps must be timezone-aware")
    return int(observed_at.timestamp()) // _PERIOD_SECONDS


def totp_at(secret: bytes, observed_at: datetime) -> str:
    """Return the six-digit TermFlow V1 code at a wall-clock timestamp."""

    return totp_for_counter(secret, counter_at(observed_at))


def match_totp_counter(secret: bytes, code: str, observed_at: datetime) -> int | None:
    """Match current or one adjacent counter without early-exit string comparison."""

    if len(code) != _V1_DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = counter_at(observed_at)
    matched: list[int] = []
    for candidate in (current - 1, current, current + 1):
        if candidate < 0:
            continue
        if hmac.compare_digest(totp_for_counter(secret, candidate), code):
            matched.append(candidate)
    return max(matched) if matched else None
