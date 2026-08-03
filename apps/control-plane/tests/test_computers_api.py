from uuid import UUID, uuid4

import pytest


def _provision_computer(client, admin_headers, *, hostname: str = "devbox"):
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    enrolled = client.post(
        "/api/v1/installations/enroll",
        json={
            "enrollment_token": enrollment,
            "hostname": hostname,
            "platform": "Linux",
            "client_version": "0.1.0",
        },
    ).json()
    return enrolled


def _provision_term(client, admin_headers, *, hostname: str = "devbox"):
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment, "hostname": hostname},
    ).json()
    instance_id = uuid4()
    instance = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "term"},
    ).json()
    return UUID(installation["installation_id"]), instance["instance_token"]


def test_list_get_and_rename_computer(client, admin_headers) -> None:
    enrolled = _provision_computer(client, admin_headers)
    installation_id = enrolled["installation_id"]

    listed = client.get("/api/v1/computers", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["computers"][0]["hostname"] == "devbox"
    assert listed.json()["computers"][0]["display_name"] == "devbox"
    assert listed.json()["computers"][0]["registered_at"].endswith("Z")

    renamed = client.patch(
        f"/api/v1/computers/{installation_id}",
        headers=admin_headers,
        json={"display_name": "开发机"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "开发机"
    detail = client.get(f"/api/v1/computers/{installation_id}", headers=admin_headers)
    assert detail.json()["display_name"] == "开发机"


@pytest.mark.parametrize("name", ["", "x" * 129, "bad\x00name", "bad\x85name"])
def test_computer_rename_validates_name(client, admin_headers, name: str) -> None:
    enrolled = _provision_computer(client, admin_headers, hostname=str(uuid4()))
    response = client.patch(
        f"/api/v1/computers/{enrolled['installation_id']}",
        headers=admin_headers,
        json={"display_name": name},
    )
    assert response.status_code == 422


def test_computer_endpoints_require_admin(client) -> None:
    assert client.get("/api/v1/computers").status_code == 401
    assert client.get(f"/api/v1/computers/{uuid4()}").status_code == 401


def test_delete_offline_computer_revokes_credentials_and_removes_it(
    client,
    admin_headers,
) -> None:
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": enrollment, "hostname": "remove-me"},
    ).json()
    installation_id = installation["installation_id"]

    deleted = client.delete(f"/api/v1/computers/{installation_id}", headers=admin_headers)

    assert deleted.status_code == 204
    assert client.get("/api/v1/computers", headers=admin_headers).json()["computers"] == []
    rejected = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(uuid4()), "name": "after-delete"},
    )
    assert rejected.status_code == 401


def test_delete_online_computer_is_rejected_without_mutation(client, admin_headers) -> None:
    installation_id, instance_token = _provision_term(client, admin_headers)

    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ):
        rejected = client.delete(
            f"/api/v1/computers/{installation_id}",
            headers=admin_headers,
        )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "computer_online"
    remaining = client.get("/api/v1/computers", headers=admin_headers).json()["computers"]
    assert remaining[0]["installation_id"] == str(installation_id)


def test_delete_unknown_or_already_deleted_computer_returns_not_found(
    client,
    admin_headers,
) -> None:
    unknown = client.delete(f"/api/v1/computers/{uuid4()}", headers=admin_headers)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "computer_not_found"

    enrolled = _provision_computer(client, admin_headers, hostname="delete-twice")
    path = f"/api/v1/computers/{enrolled['installation_id']}"
    assert client.delete(path, headers=admin_headers).status_code == 204
    repeated = client.delete(path, headers=admin_headers)
    assert repeated.status_code == 404
    assert repeated.json()["error"]["code"] == "computer_not_found"
