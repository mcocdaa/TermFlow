from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.auth.secret_box import EncryptedSecret

from .oauth_helpers import (
    approve_authorization,
    begin_authorization,
    exchange_authorization,
    key_and_jwk,
    proof,
)


def test_oauth_metadata_uses_only_configured_canonical_issuer(client) -> None:
    response = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "issuer": "http://127.0.0.1:8000",
        "authorization_endpoint": "http://127.0.0.1:8000/api/v1/oauth/authorize",
        "token_endpoint": "http://127.0.0.1:8000/api/v1/oauth/token",
        "revocation_endpoint": "http://127.0.0.1:8000/api/v1/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "dpop_signing_alg_values_supported": ["ES256"],
        "scopes_supported": [
            "terminal.read",
            "terminal.write",
            "computers.read",
            "computers.write",
        ],
    }


def test_authorization_query_rejects_credentials_extras_and_duplicate_fields(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)

    assert (
        client.get(
            "/api/v1/oauth/authorize",
            params={
                "transaction_id": transaction_id,
                "admin_token": "must-never-be-accepted-in-a-url",
            },
        ).status_code
        == 400
    )

    duplicate = client.get(
        "/api/v1/oauth/authorize",
        params=[
            ("client_name", "one"),
            ("client_name", "two"),
        ],
    )
    assert duplicate.status_code == 400


def test_native_authorization_callback_has_only_public_transaction_signal(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)
    preview = client.get(
        "/api/v1/oauth/authorize",
        params={"transaction_id": transaction_id},
    )
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json()["transaction_id"] == transaction_id
    assert preview.json()["issuer"] == "http://127.0.0.1:8000"
    assert preview.json()["key_fingerprint"]

    denied = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "deny",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
        },
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    callback = denied.json()["callback_uri"]
    assert set(parse_qs(urlsplit(callback).query)) == {"state", "transaction_id"}


def test_authorization_exchange_is_one_time_and_key_bound(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])

    dashboard_htu = "http://127.0.0.1:8000/api/v1/dashboard"
    dashboard = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu=dashboard_htu,
                nonce=nonce,
                access_token=access,
            ),
        },
    )
    assert dashboard.status_code == 200, dashboard.text

    other_key, other_jwk = key_and_jwk()
    copied = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                other_key,
                other_jwk,
                method="GET",
                htu=dashboard_htu,
                access_token=access,
            ),
        },
    )
    assert copied.status_code == 401

    replay_exchange = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu="http://127.0.0.1:8000/api/v1/oauth/token",
                nonce=dashboard.headers["dpop-nonce"],
            )
        },
        json={
            "grant_type": "authorization_code",
            "transaction_id": transaction_id,
            "code_verifier": verifier,
            "public_jwk": jwk,
        },
    )
    assert replay_exchange.status_code == 400
    assert replay_exchange.json()["error"]["code"] == "invalid_grant"


def test_native_authorization_and_token_exchange_write_secret_free_audit(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, _ = exchange_authorization(client, key, jwk, transaction_id, verifier)

    events = asyncio.run(client.app.state.repositories.auth_audit.list_all())
    assert [(event.operation, event.result) for event in events] == [
        ("native.authorization", "ok"),
        ("token.exchange", "ok"),
    ]
    rendered = repr(events)
    assert str(tokens["access_token"]) not in rendered
    assert str(tokens["refresh_token"]) not in rendered
    assert verifier not in rendered


def test_refresh_rotation_replay_revokes_the_entire_token_family(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    first, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    token_htu = "http://127.0.0.1:8000/api/v1/oauth/token"
    refresh_body = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "public_jwk": jwk,
    }
    rotated = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=token_htu, nonce=nonce)},
        json=refresh_body,
    )
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()

    replay = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_htu,
                nonce=rotated.headers["dpop-nonce"],
            )
        },
        json=refresh_body,
    )
    assert replay.status_code == 400

    dashboard = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {second['access_token']}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu="http://127.0.0.1:8000/api/v1/dashboard",
                nonce=replay.headers["dpop-nonce"],
                access_token=second["access_token"],
            ),
        },
    )
    assert dashboard.status_code == 401


def test_refresh_replay_with_a_different_key_cannot_revoke_the_bound_family(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    first, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    token_htu = "http://127.0.0.1:8000/api/v1/oauth/token"
    refresh_body = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "public_jwk": jwk,
    }
    rotated = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=token_htu, nonce=nonce)},
        json=refresh_body,
    )
    assert rotated.status_code == 200
    legitimate_nonce = rotated.headers["dpop-nonce"]

    attacker_key, attacker_jwk = key_and_jwk()
    attacker_body = {**refresh_body, "public_jwk": attacker_jwk}
    challenged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(attacker_key, attacker_jwk, method="POST", htu=token_htu)},
        json=attacker_body,
    )
    assert challenged.status_code == 401
    replay = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                attacker_key,
                attacker_jwk,
                method="POST",
                htu=token_htu,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=attacker_body,
    )
    assert replay.status_code == 400

    access = rotated.json()["access_token"]
    dashboard = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu="http://127.0.0.1:8000/api/v1/dashboard",
                nonce=legitimate_nonce,
                access_token=access,
            ),
        },
    )
    assert dashboard.status_code == 200


def test_revocation_endpoint_invalidates_the_submitted_refresh_token(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])
    revoke_htu = "http://127.0.0.1:8000/api/v1/oauth/revoke"
    revoked = client.post(
        "/api/v1/oauth/revoke",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=revoke_htu,
                nonce=nonce,
                access_token=access,
            ),
        },
        json={"token": tokens["refresh_token"], "token_type_hint": "refresh_token"},
    )
    assert revoked.status_code == 200

    refresh = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu="http://127.0.0.1:8000/api/v1/oauth/token",
                nonce=revoked.headers["dpop-nonce"],
            )
        },
        json={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "public_jwk": jwk,
        },
    )
    assert refresh.status_code == 400
    assert refresh.json()["error"]["code"] == "invalid_grant"


def test_dpop_protects_native_event_websocket(client, admin_headers) -> None:
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll", json={"enrollment_token": enrollment}
    ).json()
    instance_id = uuid4()
    client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "oauth-events"},
    )
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])
    headers = {
        "Authorization": f"Bearer {access}",
        "DPoP": proof(
            key,
            jwk,
            method="GET",
            htu="http://127.0.0.1:8000/api/v1/events",
            nonce=nonce,
            access_token=access,
        ),
    }
    with client.websocket_connect(f"/api/v1/events?instance_id={instance_id}", headers=headers):
        pass

    other_key, other_jwk = key_and_jwk()
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            f"/api/v1/events?instance_id={instance_id}",
            headers={
                "Authorization": f"Bearer {access}",
                "DPoP": proof(
                    other_key,
                    other_jwk,
                    method="GET",
                    htu="http://127.0.0.1:8000/api/v1/events",
                    access_token=access,
                ),
            },
        ):
            pass
    assert caught.value.code == 4401


def test_websocket_authentication_has_a_bounded_source_burst(client) -> None:
    close_codes: list[int] = []
    for _ in range(6):
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(
                f"/api/v1/events?instance_id={uuid4()}",
                headers={"Authorization": "Bearer invalid"},
            ):
                pass
        close_codes.append(caught.value.code)

    assert close_codes[:5] == [4401] * 5
    assert close_codes[5] == 4429


def _mark_totp_enabled(client) -> None:
    enabled = asyncio.run(
        client.app.state.repositories.auth_state.enable_totp(
            EncryptedSecret(
                ciphertext=b"encrypted-test-secret",
                nonce=b"123456789012",
                key_version=1,
                aad_version=1,
            ),
            1,
        )
    )
    assert enabled


def test_native_authorization_totp_step_up_fails_closed_without_bound_service(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)
    _mark_totp_enabled(client)

    response = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "allow",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
            "totp_code": "123456",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_native_authorization_uses_injected_fresh_totp_verifier(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)
    _mark_totp_enabled(client)
    observed: list[str] = []

    async def verify_fresh(code: str) -> bool:
        observed.append(code)
        return code == "123456"

    client.app.state.oauth_totp_verifier = verify_fresh
    response = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "allow",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
            "totp_code": "123456",
        },
    )

    assert response.status_code == 200
    assert observed == ["123456"]


def test_static_administrator_bearer_stops_authorizing_resources_when_totp_enabled(
    client,
    admin_headers,
) -> None:
    assert client.get("/api/v1/dashboard", headers=admin_headers).status_code == 200
    _mark_totp_enabled(client)

    response = client.get("/api/v1/dashboard", headers=admin_headers)

    assert response.status_code == 401


def test_native_access_token_scope_is_enforced_on_http_mutation(client, admin_headers) -> None:
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment},
    ).json()
    computer_id = installation["installation_id"]
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(
        client,
        jwk,
        scopes=("computers.read",),
    )
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])
    response = client.patch(
        f"/api/v1/computers/{computer_id}",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="PATCH",
                htu=f"http://127.0.0.1:8000/api/v1/computers/{computer_id}",
                nonce=nonce,
                access_token=access,
            ),
        },
        json={"display_name": "Blocked rename"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
