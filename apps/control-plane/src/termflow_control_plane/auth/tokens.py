"""High-entropy bearer token helpers."""

import hashlib
import hmac
import secrets


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


def secret_text_matches(supplied: str, expected: str) -> bool:
    """Compare arbitrary valid UTF-8 secrets without the ASCII-only str restriction."""

    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
