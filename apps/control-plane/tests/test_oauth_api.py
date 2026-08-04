from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.auth.dpop import DpopVerifier, jwk_thumbprint
from termflow_control_plane.auth.oauth import OAuthService
from termflow_control_plane.auth.secret_box import EncryptedSecret
from termflow_control_plane.errors import TermFlowError
from termflow_protocol import (
    OAuthAuthorizationDecisionRequest,
    OAuthPublicJwk,
)

from .oauth_helpers import (
    approve_authorization,
    b64url,
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
        "device_authorization_endpoint": "http://127.0.0.1:8000/api/v1/oauth/device/code",
        "device_verification_uri": "http://127.0.0.1:8000/device",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        "code_challenge_methods_supported": ["S256"],
        "dpop_signing_alg_values_supported": ["ES256"],
        "scopes_supported": [
            "terminal.read",
            "terminal.write",
            "computers.read",
            "computers.write",
        ],
    }


def _device_request(jwk: dict[str, str]) -> tuple[dict[str, object], str]:
    verifier = "d" * 43
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return (
        {
            "client_name": "Device C",
            "platform": "linux",
            "client_version": "1.0.0",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "dpop_jkt": jwk_thumbprint(jwk),
            "public_jwk": jwk,
            "scopes": ["terminal.read", "computers.read"],
        },
        verifier,
    )


def test_device_code_uses_configured_issuer_and_returns_short_code(client) -> None:
    _, jwk = key_and_jwk()
    body, _ = _device_request(jwk)
    response = client.post(
        "/api/v1/oauth/device/code",
        headers={"Host": "attacker.invalid"},
        json=body,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["expires_in"] == 900
    assert payload["interval"] == 5
    assert payload["verification_uri"] == "http://127.0.0.1:8000/device"
    assert payload["verification_uri_complete"].startswith("http://127.0.0.1:8000/device?code=")
    assert payload["device_code"] not in payload["verification_uri_complete"]


def test_device_user_code_resolves_the_existing_web_preview(client) -> None:
    _, jwk = key_and_jwk()
    body, _ = _device_request(jwk)
    created = client.post("/api/v1/oauth/device/code", json=body)
    assert created.status_code == 200, created.text
    user_code = created.json()["user_code"]

    preview = client.get("/api/v1/oauth/authorize", params={"user_code": user_code})
    assert preview.status_code == 200, preview.text
    assert preview.json()["client_name"] == "Device C"
    assert preview.json()["transaction_id"]
    assert preview.headers["cache-control"] == "no-store"

    mixed = client.get(
        "/api/v1/oauth/authorize",
        params={"user_code": user_code, "transaction_id": preview.json()["transaction_id"]},
    )
    assert mixed.status_code == 400


def test_device_code_approval_reuses_web_decision_and_issues_dpop_tokens(client) -> None:
    key, jwk = key_and_jwk()
    body, verifier = _device_request(jwk)
    created = client.post("/api/v1/oauth/device/code", json=body)
    assert created.status_code == 200, created.text
    device_code = created.json()["device_code"]
    authorization = asyncio.run(
        client.app.state.repositories.oauth_authorizations.find_by_device_code(
            device_code,
            epoch=1,
        )
    )
    assert authorization is not None
    transaction_id = str(authorization.id)

    preview = client.get(
        "/api/v1/oauth/authorize",
        params={"transaction_id": transaction_id},
    )
    assert preview.status_code == 200, preview.text
    approved = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "allow",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    token_url = "http://127.0.0.1:8000/api/v1/oauth/token"
    token_body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=token_url)},
        json=token_body,
    )
    assert challenged.status_code == 401, challenged.text
    exchanged = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=token_body,
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["token_type"] == "DPoP"
    replay = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=exchanged.headers["dpop-nonce"],
            )
        },
        json=token_body,
    )
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "expired_token"


def test_device_polling_reports_pending_then_slow_down(client) -> None:
    key, jwk = key_and_jwk()
    body, verifier = _device_request(jwk)
    created = client.post("/api/v1/oauth/device/code", json=body)
    assert created.status_code == 200, created.text
    token_url = "http://127.0.0.1:8000/api/v1/oauth/token"
    token_body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": created.json()["device_code"],
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=token_url)},
        json=token_body,
    )
    assert challenged.status_code == 401
    pending = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=token_body,
    )
    assert pending.status_code == 400
    assert pending.json()["error"]["code"] == "authorization_pending"
    assert pending.headers["retry-after"] == "5"
    slowed = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=pending.headers["dpop-nonce"],
            )
        },
        json=token_body,
    )
    assert slowed.status_code == 400
    assert slowed.json()["error"]["code"] == "slow_down"


def test_device_denial_and_unknown_code_are_generic_terminal_errors(client) -> None:
    key, jwk = key_and_jwk()
    body, verifier = _device_request(jwk)
    created = client.post("/api/v1/oauth/device/code", json=body)
    assert created.status_code == 200, created.text
    device_code = created.json()["device_code"]
    authorization = asyncio.run(
        client.app.state.repositories.oauth_authorizations.find_by_device_code(
            device_code,
            epoch=1,
        )
    )
    assert authorization is not None
    denied = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": str(authorization.id),
            "decision": "deny",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
        },
    )
    assert denied.status_code == 200, denied.text

    token_url = "http://127.0.0.1:8000/api/v1/oauth/token"
    token_body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "code_verifier": verifier,
        "public_jwk": jwk,
    }
    challenged = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": proof(key, jwk, method="POST", htu=token_url)},
        json=token_body,
    )
    assert challenged.status_code == 401
    access_denied = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=challenged.headers["dpop-nonce"],
            )
        },
        json=token_body,
    )
    assert access_denied.status_code == 400
    assert access_denied.json()["error"]["code"] == "access_denied"

    unknown = dict(token_body, device_code="unknown-device-code")
    challenged_unknown = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=token_url,
                nonce=access_denied.headers["dpop-nonce"],
            )
        },
        json=unknown,
    )
    assert challenged_unknown.status_code == 400
    assert challenged_unknown.json()["error"]["code"] == "expired_token"


def test_device_code_creation_has_a_bounded_source_burst(client) -> None:
    _, jwk = key_and_jwk()
    body, _ = _device_request(jwk)
    responses = [client.post("/api/v1/oauth/device/code", json=body) for _ in range(6)]
    assert [response.status_code for response in responses[:5]] == [200] * 5
    assert responses[5].status_code == 429
    assert responses[5].headers["retry-after"]


def test_device_approval_reuses_totp_step_up(client) -> None:
    _, jwk = key_and_jwk()
    body, _ = _device_request(jwk)
    created = client.post("/api/v1/oauth/device/code", json=body)
    assert created.status_code == 200, created.text
    authorization = asyncio.run(
        client.app.state.repositories.oauth_authorizations.find_by_device_code(
            created.json()["device_code"],
            epoch=1,
        )
    )
    assert authorization is not None
    _mark_totp_enabled(client)
    observed: list[str] = []

    async def verify_fresh(code: str) -> bool:
        observed.append(code)
        return code == "123456"

    client.app.state.oauth_totp_verifier = verify_fresh
    response = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": str(authorization.id),
            "decision": "allow",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
            "totp_code": "123456",
        },
    )
    assert response.status_code == 200, response.text
    assert observed == ["123456"]


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

    dashboard = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu="http://127.0.0.1:8000/api/v1/dashboard",
                nonce=revoked.headers["dpop-nonce"],
                access_token=access,
            ),
        },
    )
    assert dashboard.status_code == 401

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


def test_revocation_cannot_cross_native_client_or_key_ownership(client) -> None:
    owner_key, owner_jwk = key_and_jwk()
    owner_transaction, owner_verifier = begin_authorization(client, owner_jwk)
    approve_authorization(client, owner_transaction)
    owner_tokens, owner_nonce = exchange_authorization(
        client,
        owner_key,
        owner_jwk,
        owner_transaction,
        owner_verifier,
    )

    caller_key, caller_jwk = key_and_jwk()
    caller_transaction, caller_verifier = begin_authorization(client, caller_jwk)
    approve_authorization(client, caller_transaction)
    caller_tokens, caller_nonce = exchange_authorization(
        client,
        caller_key,
        caller_jwk,
        caller_transaction,
        caller_verifier,
    )
    caller_access = str(caller_tokens["access_token"])
    revoke_htu = "http://127.0.0.1:8000/api/v1/oauth/revoke"
    response = client.post(
        "/api/v1/oauth/revoke",
        headers={
            "Authorization": f"Bearer {caller_access}",
            "DPoP": proof(
                caller_key,
                caller_jwk,
                method="POST",
                htu=revoke_htu,
                nonce=caller_nonce,
                access_token=caller_access,
            ),
        },
        json={"token": owner_tokens["refresh_token"], "token_type_hint": "refresh_token"},
    )
    assert response.status_code == 200

    refresh_htu = "http://127.0.0.1:8000/api/v1/oauth/token"
    refresh = client.post(
        "/api/v1/oauth/token",
        headers={
            "DPoP": proof(
                owner_key,
                owner_jwk,
                method="POST",
                htu=refresh_htu,
                nonce=owner_nonce,
            )
        },
        json={
            "grant_type": "refresh_token",
            "refresh_token": owner_tokens["refresh_token"],
            "public_jwk": owner_jwk,
        },
    )
    assert refresh.status_code == 200


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
        "Authorization": f"DPoP {access}",
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


def test_native_resource_accepts_the_dpop_authorization_scheme(client) -> None:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])
    htu = "http://127.0.0.1:8000/api/v1/dashboard"

    response = client.get(
        "/api/v1/dashboard",
        headers={
            "Authorization": f"DPoP {access}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu=htu,
                nonce=nonce,
                access_token=access,
            ),
        },
    )

    assert response.status_code == 200, response.text


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
        client.app.state.repositories.auth_state.configure_totp(
            EncryptedSecret(
                ciphertext=b"encrypted-test-secret",
                nonce=b"123456789012",
                key_version=1,
                aad_version=1,
            ),
            1,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
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


def test_authorization_transaction_is_persistently_invalidated_after_five_errors(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)
    service = OAuthService(
        client.app.state.repositories,
        client.app.state.settings,
        DpopVerifier(),
    )

    async def exhaust_attempts() -> str:
        invalid = OAuthAuthorizationDecisionRequest.model_validate(
            {
                "transaction_id": transaction_id,
                "decision": "allow",
                "admin_token": "wrong-admin-token-that-is-long-enough",
            }
        )
        for _ in range(5):
            with pytest.raises(TermFlowError, match="Authentication failed"):
                await service.decide(invalid)
        valid = OAuthAuthorizationDecisionRequest.model_validate(
            {
                "transaction_id": transaction_id,
                "decision": "allow",
                "admin_token": "admin-token-that-is-long-enough-for-tests",
            }
        )
        with pytest.raises(TermFlowError) as caught:
            await service.decide(valid)
        return caught.value.code

    assert asyncio.run(exhaust_attempts()) == "authorization_expired"


def test_reauthorization_updates_the_client_management_scope_snapshot(client) -> None:
    _, jwk = key_and_jwk()
    first_transaction, _ = begin_authorization(client, jwk, scopes=("computers.read",))
    approve_authorization(client, first_transaction)
    second_transaction, _ = begin_authorization(client, jwk, scopes=("terminal.write",))
    approve_authorization(client, second_transaction)
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": "http://127.0.0.1:8000"},
        json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
    )
    assert response.status_code == 201

    listed = client.get(
        "/api/v1/admin/clients",
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert listed.status_code == 200
    assert listed.json()["clients"][0]["scopes"] == ["terminal.write"]


def test_concurrent_same_jkt_registration_reuses_one_native_client(client) -> None:
    _, jwk = key_and_jwk()
    repositories = client.app.state.repositories
    public_jwk = OAuthPublicJwk.model_validate(jwk)

    async def create_twice():
        values = {
            "display_name": "Concurrent C",
            "public_jwk": public_jwk.model_dump_json(),
            "key_thumbprint": jwk_thumbprint(jwk),
            "platform": "linux",
            "client_version": "1.0.0",
            "scopes": ("terminal.read",),
        }
        return await asyncio.gather(
            repositories.native_clients.get_or_create(**values),
            repositories.native_clients.get_or_create(**values),
        )

    first, second = asyncio.run(create_twice())
    assert first.id == second.id
    assert len(asyncio.run(repositories.native_clients.list_all())) == 1


def test_oauth_token_endpoint_is_no_store_for_missing_proof_and_invalid_body(client) -> None:
    missing_proof = client.post(
        "/api/v1/oauth/token",
        json={"grant_type": "authorization_code"},
    )
    invalid_body = client.post(
        "/api/v1/oauth/token",
        headers={"DPoP": "malformed"},
        json={"grant_type": "not-supported"},
    )

    assert missing_proof.status_code in {401, 422}
    assert missing_proof.headers["cache-control"] == "no-store"
    assert invalid_body.status_code == 422
    assert invalid_body.headers["cache-control"] == "no-store"


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


def test_instance_terminal_routes_do_not_accept_computer_management_scopes(
    client,
    admin_headers,
) -> None:
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment},
    ).json()
    instance_id = uuid4()
    registered = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "scope-instance"},
    )
    assert registered.status_code == 201

    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(
        client,
        jwk,
        scopes=("computers.read", "computers.write"),
    )
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    access = str(tokens["access_token"])

    topology_path = f"/api/v1/instances/{instance_id}/topology"
    topology = client.get(
        topology_path,
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="GET",
                htu=f"http://127.0.0.1:8000{topology_path}",
                nonce=nonce,
                access_token=access,
            ),
        },
    )
    assert topology.status_code == 403
    assert topology.json()["error"]["code"] == "insufficient_scope"

    input_path = f"/api/v1/instances/{instance_id}/panes/%1/input"
    pane_input = client.post(
        input_path,
        headers={
            "Authorization": f"Bearer {access}",
            "DPoP": proof(
                key,
                jwk,
                method="POST",
                htu=f"http://127.0.0.1:8000{input_path}",
                nonce=nonce,
                access_token=access,
            ),
            "Idempotency-Key": str(uuid4()),
        },
        json={"text": "whoami", "submit": True},
    )
    assert pane_input.status_code == 403
    assert pane_input.json()["error"]["code"] == "insufficient_scope"
