from .oauth_helpers import (
    approve_authorization,
    begin_authorization,
    exchange_authorization,
    key_and_jwk,
)

ORIGIN = "http://127.0.0.1:8000"


def _register_client(client) -> str:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    exchange_authorization(client, key, jwk, transaction_id, verifier)
    return transaction_id


def _web_login(client) -> None:
    response = client.post(
        "/api/v1/admin/sessions",
        headers={"Origin": ORIGIN},
        json={"admin_token": "admin-token-that-is-long-enough-for-tests"},
    )
    assert response.status_code == 201


def test_native_clients_are_managed_only_by_same_origin_web_session(client, admin_headers) -> None:
    _register_client(client)

    assert client.get("/api/v1/admin/clients", headers=admin_headers).status_code == 401
    _web_login(client)
    listed = client.get("/api/v1/admin/clients")
    assert listed.status_code == 200
    [registered] = listed.json()["clients"]
    assert registered["display_name"] == "Desktop C"
    client_id = registered["client_id"]

    assert client.patch(
        f"/api/v1/admin/clients/{client_id}",
        headers={"Origin": "https://evil.example"},
        json={"display_name": "Renamed C"},
    ).status_code == 403
    renamed = client.patch(
        f"/api/v1/admin/clients/{client_id}",
        headers={"Origin": ORIGIN},
        json={"display_name": "Renamed C"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Renamed C"

    deleted = client.delete(
        f"/api/v1/admin/clients/{client_id}", headers={"Origin": ORIGIN}
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert client.delete(
        f"/api/v1/admin/clients/{client_id}", headers={"Origin": ORIGIN}
    ).status_code == 404


def test_denied_or_abandoned_authorization_is_not_an_authorized_client(client) -> None:
    _, jwk = key_and_jwk()
    transaction_id, _ = begin_authorization(client, jwk)
    denied = client.post(
        "/api/v1/oauth/authorize",
        json={
            "transaction_id": transaction_id,
            "decision": "deny",
            "admin_token": "admin-token-that-is-long-enough-for-tests",
        },
    )
    assert denied.status_code == 200
    _web_login(client)

    listed = client.get("/api/v1/admin/clients")

    assert listed.status_code == 200
    assert listed.json() == {"clients": []}
