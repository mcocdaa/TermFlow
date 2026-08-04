from __future__ import annotations

import base64
import hashlib
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

pytestmark = pytest.mark.e2e


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _native_key() -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    return key, {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def _dpop(
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
    *,
    htu: str,
    nonce: str | None = None,
) -> str:
    claims: dict[str, str | int] = {
        "jti": f"device-e2e-proof-{time.time_ns()}",
        "htm": "POST",
        "htu": htu,
        "iat": int(time.time()),
    }
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(
        claims,
        key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk},
    )


def _device_code_request(jwk: dict[str, str]) -> tuple[dict[str, object], str]:
    verifier = "d" * 43
    thumbprint = _b64url(
        hashlib.sha256(
            json.dumps(
                {"crv": "P-256", "kty": "EC", "x": jwk["x"], "y": jwk["y"]},
                separators=(",", ":"),
            ).encode()
        ).digest()
    )
    return (
        {
            "client_name": "Device authorization E2E",
            "platform": "test",
            "client_version": "1.0.0",
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "dpop_jkt": thumbprint,
            "public_jwk": jwk,
            "scopes": ["terminal.read", "computers.read"],
        },
        verifier,
    )


def _exchange_device_code(
    native: httpx.Client,
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
    *,
    device_code: str,
    verifier: str,
) -> httpx.Response:
    token_path = "/api/v1/oauth/token"
    token_url = f"{native.base_url}{token_path}"
    body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = native.post(
        token_path,
        headers={"DPoP": _dpop(key, jwk, htu=token_url)},
        json=body,
    )
    assert challenged.status_code == 401, challenged.text
    return native.post(
        token_path,
        headers={
            "DPoP": _dpop(
                key,
                jwk,
                htu=token_url,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=body,
    )


def test_real_process_device_code_is_approved_by_a_separate_web_session(
    termflow_system,
) -> None:
    origin = {"Origin": termflow_system.base_url}
    with (
        httpx.Client(base_url=termflow_system.base_url, timeout=2) as native,
        httpx.Client(
            base_url=termflow_system.base_url,
            headers=origin,
            timeout=2,
        ) as web,
    ):
        key, jwk = _native_key()
        request, verifier = _device_code_request(jwk)
        created = native.post("/api/v1/oauth/device/code", json=request)
        assert created.status_code == 200, created.text
        assert created.headers["cache-control"] == "no-store"
        device = created.json()
        assert device["expires_in"] == 15 * 60
        assert device["device_code"] not in device["verification_uri_complete"]

        logged_in = web.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        assert logged_in.status_code == 201, logged_in.text
        preview = web.get(
            "/api/v1/oauth/authorize",
            params={"user_code": device["user_code"]},
        )
        assert preview.status_code == 200, preview.text
        approved = web.post(
            "/api/v1/oauth/authorize",
            json={"transaction_id": preview.json()["transaction_id"], "decision": "allow"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        token = _exchange_device_code(
            native,
            key,
            jwk,
            device_code=device["device_code"],
            verifier=verifier,
        )
        assert token.status_code == 200, token.text
        assert token.headers["cache-control"] == "no-store"
        assert token.json()["access_token"]

        denied = native.post("/api/v1/oauth/device/code", json=request)
        assert denied.status_code == 200, denied.text
        denial_preview = web.get(
            "/api/v1/oauth/authorize",
            params={"user_code": denied.json()["user_code"]},
        )
        assert denial_preview.status_code == 200, denial_preview.text
        denial = web.post(
            "/api/v1/oauth/authorize",
            json={"transaction_id": denial_preview.json()["transaction_id"], "decision": "deny"},
        )
        assert denial.status_code == 200, denial.text
        assert denial.json()["status"] == "denied"

        rejected = _exchange_device_code(
            native,
            key,
            jwk,
            device_code=denied.json()["device_code"],
            verifier=verifier,
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["error"]["code"] == "access_denied"
