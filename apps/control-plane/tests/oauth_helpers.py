from __future__ import annotations

import base64
import hashlib
import itertools
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from termflow_control_plane.auth.dpop import jwk_thumbprint

_IDS = itertools.count()


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def key_and_jwk() -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }
    return key, jwk


def proof(
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
    *,
    method: str,
    htu: str,
    nonce: str | None = None,
    access_token: str | None = None,
) -> str:
    claims: dict[str, str | int] = {
        "jti": f"proof-{next(_IDS):016d}",
        "htm": method.upper(),
        "htu": htu,
        "iat": int(datetime.now(UTC).timestamp()),
    }
    if nonce is not None:
        claims["nonce"] = nonce
    if access_token is not None:
        claims["ath"] = b64url(hashlib.sha256(access_token.encode()).digest())
    return jwt.encode(
        claims,
        key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk},
    )


def begin_authorization(
    client,
    jwk: dict[str, str],
    *,
    scopes: tuple[str, ...] = (
        "terminal.read",
        "terminal.write",
        "computers.read",
        "computers.write",
    ),
) -> tuple[str, str]:
    state = "state-value-that-is-long-enough-1234"
    verifier = "v" * 43
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    params: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_name", "Desktop C"),
        ("platform", "linux"),
        ("client_version", "1.0.0"),
        ("redirect_uri", "termflow://auth/callback"),
        ("state", state),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("dpop_jkt", jwk_thumbprint(jwk)),
        ("public_jwk", json.dumps(jwk, separators=(",", ":"))),
    ]
    params.extend(("scopes", scope) for scope in scopes)
    response = client.get(
        "/api/v1/oauth/authorize",
        params=params,
        follow_redirects=False,
    )
    assert response.status_code == 307, response.text
    location = response.headers["location"]
    parsed = urlsplit(location)
    assert parsed.path == "/authorize"
    query = parse_qs(parsed.query)
    assert set(query) == {"transaction_id"}
    return query["transaction_id"][0], verifier


def approve_authorization(client, transaction_id: str) -> str:
    response = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "allow",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
        },
    )
    assert response.status_code == 200, response.text
    callback = response.json()["callback_uri"]
    query = parse_qs(urlsplit(callback).query)
    assert set(query) == {"state", "transaction_id"}
    assert query["transaction_id"] == [transaction_id]
    assert "code" not in callback and "token" not in callback
    return callback


def exchange_authorization(
    client,
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
    transaction_id: str,
    verifier: str,
) -> tuple[dict[str, object], str]:
    htu = "http://127.0.0.1:8000/api/v1/oauth/token"
    body = {
        "grant_type": "authorization_code",
        "transaction_id": transaction_id,
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=htu)},
        json=body,
    )
    assert challenged.status_code == 401, challenged.text
    assert challenged.json()["error"]["code"] == "use_dpop_nonce"
    nonce = challenged.headers["dpop-nonce"]
    exchanged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=htu, nonce=nonce)},
        json=body,
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["token_type"] == "DPoP"
    return exchanged.json(), exchanged.headers["dpop-nonce"]
