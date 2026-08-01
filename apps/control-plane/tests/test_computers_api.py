from uuid import uuid4

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
