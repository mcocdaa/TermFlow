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


def test_jwk_thumbprint_uses_rfc7638_required_members_only() -> None:
    _, jwk = _key_and_jwk()
    with_optional = {**jwk, "use": "sig", "kid": "ignored"}

    assert jwk_thumbprint(with_optional) == jwk_thumbprint(jwk)
    assert len(jwk_thumbprint(jwk)) == 43


def test_dpop_requires_nonce_then_verifies_exact_request_and_rotates_nonce() -> None:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    key, jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now)
    jkt = jwk_thumbprint(jwk)

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

    proof = _proof(key, jwk, now=now, nonce=first.value.nonce)
    verified = verifier.verify(
        proof,
        method="GET",
        htu="https://b.example/api/v1/dashboard?ignored=1",
        expected_jkt=jkt,
    )
    assert verified.jkt == jkt
    assert verified.next_nonce != first.value.nonce

    with pytest.raises(DpopInvalid, match="replayed"):
        verifier.verify(
            proof,
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )


@pytest.mark.parametrize(
    ("method", "htu", "offset"),
    [
        ("POST", "https://b.example/api/v1/dashboard", 0),
        ("GET", "https://evil.example/api/v1/dashboard", 0),
        ("GET", "https://b.example/api/v1/dashboard", 121),
    ],
)
def test_dpop_rejects_wrong_method_url_or_stale_iat(
    method: str,
    htu: str,
    offset: int,
) -> None:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    key, jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now)
    jkt = jwk_thumbprint(jwk)
    with pytest.raises(DpopNonceRequired) as challenge:
        verifier.verify(
            _proof(key, jwk, now=now, nonce=None),
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )
    proof = _proof(
        key,
        jwk,
        now=now + timedelta(seconds=offset),
        nonce=challenge.value.nonce,
        method=method,
        htu=htu,
    )

    with pytest.raises(DpopInvalid):
        verifier.verify(
            proof,
            method="GET",
            htu="https://b.example/api/v1/dashboard",
            expected_jkt=jkt,
        )


def test_resource_proof_requires_access_token_hash_and_bound_key() -> None:
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


def test_websocket_proofs_can_reuse_nonce_with_fresh_jti_when_no_response_header_exists() -> None:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    key, jwk = _key_and_jwk()
    verifier = DpopVerifier(clock=lambda: now)
    jkt = jwk_thumbprint(jwk)
    with pytest.raises(DpopNonceRequired) as challenge:
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
            nonce=challenge.value.nonce,
            jti="websocket-proof-one",
            htu="https://b.example/api/v1/events",
        ),
        method="GET",
        htu="https://b.example/api/v1/events",
        expected_jkt=jkt,
        rotate_nonce=False,
    )
    second = verifier.verify(
        _proof(
            key,
            jwk,
            now=now,
            nonce=challenge.value.nonce,
            jti="websocket-proof-two",
            htu="https://b.example/api/v1/events",
        ),
        method="GET",
        htu="https://b.example/api/v1/events",
        expected_jkt=jkt,
        rotate_nonce=False,
    )

    assert first.next_nonce == challenge.value.nonce
    assert second.next_nonce == challenge.value.nonce
