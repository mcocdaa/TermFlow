"""RFC 9449 DPoP proof validation with bounded replay and nonce state."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import InvalidTokenError


class DpopInvalid(ValueError):
    """A proof is malformed, incorrectly bound, stale, or replayed."""


class DpopNonceRequired(DpopInvalid):
    """The caller must retry once using the returned server nonce."""

    def __init__(self, nonce: str) -> None:
        super().__init__("a fresh DPoP nonce is required")
        self.nonce = nonce


@dataclass(frozen=True, slots=True)
class VerifiedDpop:
    jkt: str
    jti: str
    next_nonce: str


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_coordinate(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 43 or "=" in value:
        raise DpopInvalid("DPoP public key coordinate is malformed")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DpopInvalid("DPoP public key coordinate is malformed") from exc
    if len(decoded) != 32 or _b64url(decoded) != value:
        raise DpopInvalid("DPoP public key coordinate is malformed")
    return decoded


def _validated_jwk(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise DpopInvalid("DPoP proof has no public key")
    if "d" in value:
        raise DpopInvalid("DPoP proof contains private key material")
    required = {"kty": "EC", "crv": "P-256", "alg": "ES256"}
    if any(value.get(name) != expected for name, expected in required.items()):
        raise DpopInvalid("DPoP proof uses an unsupported public key")
    x = value.get("x")
    y = value.get("y")
    _decode_coordinate(x)
    _decode_coordinate(y)
    assert isinstance(x, str) and isinstance(y, str)
    return {**required, "x": x, "y": y}


def jwk_thumbprint(jwk: Mapping[str, object]) -> str:
    """Return the RFC 7638 thumbprint for a P-256 public JWK."""

    public = _validated_jwk(jwk)
    canonical = json.dumps(
        {
            "crv": public["crv"],
            "kty": public["kty"],
            "x": public["x"],
            "y": public["y"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def canonicalize_htu(value: str) -> str:
    """Normalize a configured absolute target while excluding query/fragment."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DpopInvalid("DPoP target URI is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DpopInvalid("DPoP target URI is malformed")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", "", ""))


class DpopVerifier:
    """Validate proofs and hold only bounded, expiring nonce/JTI metadata."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        iat_tolerance: timedelta = timedelta(seconds=120),
        nonce_ttl: timedelta = timedelta(minutes=5),
        replay_ttl: timedelta = timedelta(minutes=5),
        capacity: int = 8192,
    ) -> None:
        if iat_tolerance <= timedelta(0) or nonce_ttl <= timedelta(0):
            raise ValueError("DPoP time windows must be positive")
        if replay_ttl <= timedelta(0) or capacity < 1:
            raise ValueError("DPoP replay configuration is invalid")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._iat_tolerance = iat_tolerance
        self._nonce_ttl = nonce_ttl
        self._replay_ttl = replay_ttl
        self._capacity = capacity
        self._nonces: OrderedDict[str, tuple[str, datetime]] = OrderedDict()
        self._jtis: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._lock = Lock()

    def __repr__(self) -> str:
        return (
            f"DpopVerifier(nonce_count={len(self._nonces)}, "
            f"replay_count={len(self._jtis)}, capacity={self._capacity})"
        )

    def challenge(self, jkt: str) -> str:
        now = self._clock()
        with self._lock:
            self._prune(now)
            current = self._nonces.get(jkt)
            if current is not None and current[1] > now:
                self._nonces.move_to_end(jkt)
                return current[0]
            return self._rotate_nonce(jkt, now)

    def verify(
        self,
        proof: str,
        *,
        method: str,
        htu: str,
        expected_jkt: str | None = None,
        access_token: str | None = None,
        rotate_nonce: bool = True,
    ) -> VerifiedDpop:
        now = self._clock()
        try:
            header = jwt.get_unverified_header(proof)
        except InvalidTokenError as exc:
            raise DpopInvalid("DPoP proof is malformed") from exc
        if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
            raise DpopInvalid("DPoP proof header is invalid")
        jwk = _validated_jwk(header.get("jwk"))
        jkt = jwk_thumbprint(jwk)
        if expected_jkt is not None and not hmac.compare_digest(jkt, expected_jkt):
            raise DpopInvalid("DPoP proof key is not bound to the credential")
        public_numbers = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_decode_coordinate(jwk["x"]), "big"),
            int.from_bytes(_decode_coordinate(jwk["y"]), "big"),
            ec.SECP256R1(),
        )
        try:
            payload: dict[str, Any] = jwt.decode(
                proof,
                public_numbers.public_key(),
                algorithms=["ES256"],
                options={
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except (InvalidTokenError, ValueError) as exc:
            raise DpopInvalid("DPoP proof signature is invalid") from exc
        jti = payload.get("jti")
        issued_at = payload.get("iat")
        if not isinstance(jti, str) or len(jti) < 8 or len(jti) > 256:
            raise DpopInvalid("DPoP proof jti is invalid")
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            raise DpopInvalid("DPoP proof iat is invalid")
        if abs(now.timestamp() - issued_at) > self._iat_tolerance.total_seconds():
            raise DpopInvalid("DPoP proof is stale")
        if payload.get("htm") != method.upper():
            raise DpopInvalid("DPoP proof method does not match")
        proof_htu = payload.get("htu")
        if (
            not isinstance(proof_htu, str)
            or urlsplit(proof_htu).query
            or urlsplit(proof_htu).fragment
        ):
            raise DpopInvalid("DPoP proof target does not match")
        if not hmac.compare_digest(canonicalize_htu(proof_htu), canonicalize_htu(htu)):
            raise DpopInvalid("DPoP proof target does not match")
        expected_ath = (
            _b64url(hashlib.sha256(access_token.encode("utf-8")).digest())
            if access_token is not None
            else None
        )
        actual_ath = payload.get("ath")
        if expected_ath is None:
            if actual_ath is not None:
                raise DpopInvalid("DPoP token proof must not include ath")
        elif not isinstance(actual_ath, str) or not hmac.compare_digest(actual_ath, expected_ath):
            raise DpopInvalid("DPoP access-token hash does not match")

        with self._lock:
            self._prune(now)
            replay_key = (jkt, jti)
            if replay_key in self._jtis:
                raise DpopInvalid("DPoP proof was replayed")
            current = self._nonces.get(jkt)
            supplied_nonce = payload.get("nonce")
            if (
                current is None
                or current[1] <= now
                or not isinstance(supplied_nonce, str)
                or not hmac.compare_digest(supplied_nonce, current[0])
            ):
                nonce = (
                    current[0]
                    if current is not None and current[1] > now
                    else self._rotate_nonce(jkt, now)
                )
                raise DpopNonceRequired(nonce)
            self._jtis[replay_key] = now + self._replay_ttl
            self._jtis.move_to_end(replay_key)
            next_nonce = self._rotate_nonce(jkt, now) if rotate_nonce else current[0]
            return VerifiedDpop(jkt=jkt, jti=jti, next_nonce=next_nonce)

    def _rotate_nonce(self, jkt: str, now: datetime) -> str:
        nonce = secrets.token_urlsafe(32)
        self._nonces[jkt] = (nonce, now + self._nonce_ttl)
        self._nonces.move_to_end(jkt)
        while len(self._nonces) > self._capacity:
            self._nonces.popitem(last=False)
        return nonce

    def _prune(self, now: datetime) -> None:
        expired_nonces = [
            nonce_key for nonce_key, (_, expires_at) in self._nonces.items() if expires_at <= now
        ]
        for nonce_key in expired_nonces:
            del self._nonces[nonce_key]
        expired_jtis = [
            replay_key for replay_key, expires_at in self._jtis.items() if expires_at <= now
        ]
        for replay_key in expired_jtis:
            del self._jtis[replay_key]
        while len(self._jtis) > self._capacity:
            self._jtis.popitem(last=False)
