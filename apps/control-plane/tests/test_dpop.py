from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from termflow_control_plane.auth.dpop import (
    DpopInvalid,
    DpopNonceRequired,
    DpopVerifier,
    jwk_thumbprint,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _key_and_jwk() -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": _b64(numbers.x.to_bytes(32, "big")),
        "y": _b64(numbers.y.to_bytes(32, "big")),
    }
    return key, jwk


def _proof(
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
    *,
    now: datetime,
    nonce: str | None,
    jti: str = "proof-id-12345678",
    method: str = "GET",
    htu: str = "https://b.example/api/v1/dashboard",
    access_token: str | None = None,
) -> str:
    payload: dict[str, str | int] = {
        "jti": jti,
        "htm": method,
        "htu": htu,
        "iat": int(now.timestamp()),
    }
    if nonce is not None:
        payload["nonce"] = nonce
    if access_token is not None:
        payload["ath"] = _b64(hashlib.sha256(access_token.encode()).digest())
    return jwt.encode(
        payload,
        key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk},
    )


def test_dpop_nonce_verification_flow() -> None:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    key, jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now)
    jkt = jwk_thumbprint(jwk)
    assert len(jkt) == 43

    # First proof without a nonce yields a DPoP-Nonce challenge.
    with pytest.raises(DpopNonceRequired) as first:
        verifier.verify(
            _proof(
                key,
                jwk,
                now=now,
                nonce=None,
                htu="https://b.example/api/v1/dashboard",
            ),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )

    # The nonce-bound proof verifies, query components are ignored, and the
    # nonce stays stable until it approaches expiry so parallel requests can
    # share it.
    proof = _proof(key, jwk, now=now, nonce=first.value.nonce)
    verified = verifier.verify(
        proof,
        method="GET",
        htu="https://b.example/api/v1/dashboard?ignored=1",
        expected_jkt=jkt,
    )
    assert verified.jkt == jkt
    assert verified.next_nonce == first.value.nonce

    # A replay of the exact same proof is rejected.
    with pytest.raises(DpopInvalid, match="replayed"):
        verifier.verify(
            proof,
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )


def test_dpop_nonce_rotates_only_near_expiry() -> None:
    """Concurrent native requests share one cached nonce: a rotation on every
    response would make them invalidate each other (401 churn)."""

    now = [datetime(2026, 8, 2, 8, tzinfo=UTC)]
    key, jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now[0])
    jkt = jwk_thumbprint(jwk)

    with pytest.raises(DpopNonceRequired) as challenge:
        verifier.verify(
            _proof(key, jwk, now=now[0], nonce=None),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )
    nonce = challenge.value.nonce

    def verify(jti: str) -> str:
        return verifier.verify(
            _proof(key, jwk, now=now[0], nonce=nonce, jti=jti),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        ).next_nonce

    # Early in the nonce lifetime every parallel request reuses it unchanged.
    assert verify("parallel-request-1") == nonce
    now[0] += timedelta(minutes=1)
    assert verify("parallel-request-2") == nonce

    # Past half of the TTL the next success hands out a fresh nonce.
    now[0] += timedelta(minutes=2)
    rotated = verify("parallel-request-3")
    assert rotated != nonce

    with pytest.raises(DpopNonceRequired) as reissued:
        verifier.verify(
            _proof(key, jwk, now=now[0], nonce=nonce, jti="stale-nonce-request"),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )
    assert reissued.value.nonce == rotated


def test_dpop_rejects_bad_proofs_and_verifies_resource_and_websocket_proofs() -> None:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    key, jwk = _key_and_jwk()
    other_key, other_jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now)
    jkt = jwk_thumbprint(jwk)
    with pytest.raises(DpopNonceRequired) as challenge:
        verifier.verify(
            _proof(key, jwk, now=now, nonce=None),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )

    # Wrong method, wrong target URL, and stale iat are all rejected.
    for method, htu, offset in (
        ("POST", "https://b.example/api/v1/dashboard", 0),
        ("GET", "https://evil.example/api/v1/dashboard", 0),
        ("GET", "https://b.example/api/v1/dashboard", 121),
    ):
        with pytest.raises(DpopInvalid):
            verifier.verify(
                _proof(
                    key,
                    jwk,
                    now=now + timedelta(seconds=offset),
                    nonce=challenge.value.nonce,
                    method=method,
                    htu=htu,
                ),
                method="GET",
                htu="https://b.example/api/v1/dashboard",
                expected_jkt=jkt,
            )

    # Resource proofs must carry the access-token hash of the token they
    # protect...
    with pytest.raises(DpopInvalid):
        verifier.verify(
            _proof(
                key,
                jwk,
                now=now,
                nonce=challenge.value.nonce,
                access_token="wrong-token",
            ),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
            access_token="right-token",
        )

    # ...and must be signed by the key the token is bound to.
    with pytest.raises(DpopInvalid):
        verifier.verify(
            _proof(
                other_key,
                other_jwk,
                now=now,
                nonce=challenge.value.nonce,
                access_token="right-token",
            ),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
            access_token="right-token",
        )

    # WebSocket proofs (no response header exists to rotate the nonce) may
    # reuse the nonce as long as each proof carries a fresh jti.
    ws_challenge: DpopNonceRequired
    with pytest.raises(DpopNonceRequired) as ws_challenge:
        verifier.verify(
            _proof(
                key,
                jwk,
                now=now,
                nonce=None,
                htu="https://b.example/api/v1/events",
            ),
            method="GET",
            htu="https://b.example/api/v1/events",
            expected_jkt=jkt,
        )

    first = verifier.verify(
        _proof(
            key,
            jwk,
            now=now,
            nonce=ws_challenge.value.nonce,
            jti="websocket-proof-one",
            htu="https://b.example/api/v1/events",
        ),
        method="GET",
        htu="https://b.example/api/v1/events",
        expected_jkt=jkt,
    )
    second = verifier.verify(
        _proof(
            key,
            jwk,
            now=now,
            nonce=ws_challenge.value.nonce,
            jti="websocket-proof-two",
            htu="https://b.example/api/v1/events",
        ),
        method="GET",
        htu="https://b.example/api/v1/events",
        expected_jkt=jkt,
    )

    assert first.next_nonce == ws_challenge.value.nonce
    assert second.next_nonce == ws_challenge.value.nonce
