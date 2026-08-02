from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec


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
    method: str,
    htu: str,
    nonce: str | None = None,
    access_token: str | None = None,
) -> str:
    claims: dict[str, str | int] = {
        "jti": f"e2e-proof-{time.time_ns()}",
        "htm": method.upper(),
        "htu": htu,
        "iat": int(time.time()),
    }
    if nonce is not None:
        claims["nonce"] = nonce
    if access_token is not None:
        claims["ath"] = _b64url(hashlib.sha256(access_token.encode()).digest())
    return jwt.encode(
        claims,
        key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk},
    )


def _native_authorize(
    client: httpx.Client,
    key: ec.EllipticCurvePrivateKey,
    jwk: dict[str, str],
) -> tuple[dict[str, object], str]:
    verifier = "v" * 43
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    canonical_jwk = json.dumps(jwk, separators=(",", ":"))
    thumbprint = _b64url(
        hashlib.sha256(
            json.dumps(
                {"crv": "P-256", "kty": "EC", "x": jwk["x"], "y": jwk["y"]},
                separators=(",", ":"),
            ).encode()
        ).digest()
    )
    response = client.get(
        "/api/v1/oauth/authorize",
        params={
            "response_type": "code",
            "client_name": "E2E native",
            "platform": "linux",
            "client_version": "1.0.0",
            "redirect_uri": "termflow://auth/callback",
            "state": "e2e-state-value-1234567890",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "dpop_jkt": thumbprint,
            "public_jwk": canonical_jwk,
            "scopes": ["terminal.read", "computers.read"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 307, response.text
    transaction_id = parse_qs(urlsplit(response.headers["location"]).query)["transaction_id"][0]
    approved = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "allow",
            "admin_token": "e2e-admin-token-that-is-long-enough",
        },
    )
    assert approved.status_code == 200, approved.text
    token_path = "/api/v1/oauth/token"
    token_url = f"{client.base_url}{token_path}"
    body = {
        "grant_type": "authorization_code",
        "transaction_id": transaction_id,
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = client.post(
        token_path,
        headers={"DPoP": _dpop(key, jwk, method="POST", htu=token_url)},
        json=body,
    )
    assert challenged.status_code == 401, challenged.text
    exchanged = client.post(
        token_path,
        headers={
            "DPoP": _dpop(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=body,
    )
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json(), exchanged.headers["dpop-nonce"]


def _totp_code(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return f"{value % 1_000_000:06d}"


def test_real_process_totp_enable_login_replay_and_disable(
    termflow_system,
) -> None:
    origin = {"Origin": termflow_system.base_url}
    with httpx.Client(base_url=termflow_system.base_url, headers=origin, timeout=2) as web:
        login = web.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        assert login.status_code == 201, login.text

        status = web.get("/api/v1/admin/totp")
        assert status.status_code == 200, status.text
        assert status.json() == {"enabled": False, "available": True}

        setup = web.post(
            "/api/v1/admin/totp/setups",
            json={"admin_token": termflow_system.admin_token},
        )
        assert setup.status_code == 201, setup.text
        assert setup.headers["cache-control"] == "no-store"
        setup_body = setup.json()
        secret = base64.b32decode(setup_body["setup_key"])
        initial_counter = int(time.time()) // 30
        confirm = web.post(
            f"/api/v1/admin/totp/setups/{setup_body['setup_id']}/confirm",
            json={"code": _totp_code(secret, initial_counter - 1)},
        )
        assert confirm.status_code == 200, confirm.text

        logout = web.delete("/api/v1/admin/session")
        assert logout.status_code == 200, logout.text
        first_challenge = web.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        second_challenge = web.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        assert first_challenge.status_code == 202, first_challenge.text
        assert second_challenge.status_code == 202, second_challenge.text

        current_code = _totp_code(secret, initial_counter)
        completed = web.post(
            f"/api/v1/admin/sessions/{first_challenge.json()['challenge_id']}/totp",
            json={"code": current_code},
        )
        assert completed.status_code == 201, completed.text
        replayed = web.post(
            f"/api/v1/admin/sessions/{second_challenge.json()['challenge_id']}/totp",
            json={"code": current_code},
        )
        assert replayed.status_code == 401, replayed.text
        assert replayed.json()["error"]["code"] == "authentication_failed"

        disabled = web.request(
            "DELETE",
            "/api/v1/admin/totp",
            json={
                "admin_token": termflow_system.admin_token,
                "code": _totp_code(secret, initial_counter + 1),
            },
        )
        assert disabled.status_code == 204, disabled.text
        assert web.get("/api/v1/admin/totp").json() == {
            "enabled": False,
            "available": True,
        }

        assert web.delete("/api/v1/admin/session").status_code == 200
        direct_login = web.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        assert direct_login.status_code == 201, direct_login.text


def test_real_process_native_dpop_rotation_replay_and_key_binding(termflow_system) -> None:
    with httpx.Client(base_url=termflow_system.base_url, timeout=2) as client:
        key, jwk = _native_key()
        first, nonce = _native_authorize(client, key, jwk)
        access = str(first["access_token"])
        dashboard_path = "/api/v1/dashboard"
        dashboard_url = f"{termflow_system.base_url}{dashboard_path}"
        dashboard = client.get(
            dashboard_path,
            headers={
                "Authorization": f"DPoP {access}",
                "DPoP": _dpop(
                    key,
                    jwk,
                    method="GET",
                    htu=dashboard_url,
                    nonce=nonce,
                    access_token=access,
                ),
            },
        )
        assert dashboard.status_code == 200, dashboard.text

        other_key, other_jwk = _native_key()
        copied = client.get(
            dashboard_path,
            headers={
                "Authorization": f"DPoP {access}",
                "DPoP": _dpop(
                    other_key,
                    other_jwk,
                    method="GET",
                    htu=dashboard_url,
                    access_token=access,
                ),
            },
        )
        assert copied.status_code == 401

        token_path = "/api/v1/oauth/token"
        token_url = f"{termflow_system.base_url}{token_path}"
        refresh_body = {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "public_jwk": jwk,
        }
        rotated = client.post(
            token_path,
            headers={
                "DPoP": _dpop(
                        key,
                        jwk,
                        method="POST",
                        htu=token_url,
                        nonce=dashboard.headers["dpop-nonce"],
                )
            },
            json=refresh_body,
        )
        assert rotated.status_code == 200, rotated.text
        second_access = str(rotated.json()["access_token"])
        replay = client.post(
            token_path,
            headers={
                "DPoP": _dpop(
                    key,
                    jwk,
                    method="POST",
                    htu=token_url,
                    nonce=rotated.headers["dpop-nonce"],
                )
            },
            json=refresh_body,
        )
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "invalid_grant"
        revoked = client.get(
            dashboard_path,
            headers={
                "Authorization": f"DPoP {second_access}",
                "DPoP": _dpop(
                    key,
                    jwk,
                    method="GET",
                    htu=dashboard_url,
                    nonce=replay.headers["dpop-nonce"],
                    access_token=second_access,
                ),
            },
        )
        assert revoked.status_code == 401


def test_real_process_cli_reset_revokes_web_session(termflow_system) -> None:
    with httpx.Client(base_url=termflow_system.base_url, timeout=2) as web:
        origin = {"Origin": termflow_system.base_url}
        login = web.post(
            "/api/v1/admin/sessions",
            headers=origin,
            json={"admin_token": termflow_system.admin_token},
        )
        assert login.status_code == 201, login.text
        assert web.get("/api/v1/admin/session", headers=origin).status_code == 200
        result = subprocess.run(
            [str(termflow_system.repo / ".venv/bin/termflow-control"), "auth", "totp", "reset"],
            input="y\n",
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "TERMFLOW_ADMIN_TOKEN": termflow_system.admin_token,
                "TERMFLOW_DATABASE_URL": f"sqlite+aiosqlite:///{termflow_system.database_path}",
            },
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if web.get("/api/v1/admin/session", headers=origin).status_code == 401:
                break
            time.sleep(0.05)
        assert web.get("/api/v1/admin/session", headers=origin).status_code == 401
