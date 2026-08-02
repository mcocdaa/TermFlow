from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

import httpx


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
