"""RFC 7636 S256 proof-key helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

_PKCE_VALUE = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", flags=re.ASCII)


def create_s256_challenge(verifier: str) -> str:
    """Return the canonical unpadded base64url SHA-256 challenge."""

    if _PKCE_VALUE.fullmatch(verifier) is None:
        raise ValueError("PKCE verifier is malformed")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_s256(verifier: str, expected_challenge: str) -> bool:
    """Verify a canonical S256 challenge without timing-dependent equality."""

    if _PKCE_VALUE.fullmatch(verifier) is None:
        return False
    if len(expected_challenge) != 43 or "=" in expected_challenge:
        return False
    return hmac.compare_digest(create_s256_challenge(verifier), expected_challenge)
