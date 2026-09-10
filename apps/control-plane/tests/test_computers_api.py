from uuid import uuid4


def test_list_get_and_rename_computer(client, admin_headers, provision_computer) -> None:
    assert client.get("/api/v1/computers").status_code == 401
    assert client.get(f"/api/v1/computers/{uuid4()}").status_code == 401

    enrolled = provision_computer(
        hostname="devbox",
        platform="Linux",
        client_version="0.1.0",
    )
    installation_id = enrolled.installation_id

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


def test_delete_offline_computer_revokes_credentials_and_removes_it(
    client,
    admin_headers,
    provision_computer,
) -> None:
    installation = provision_computer(hostname="remove-me")
    installation_id = installation.installation_id

    deleted = client.delete(f"/api/v1/computers/{installation_id}", headers=admin_headers)

    assert deleted.status_code == 202
    assert client.get("/api/v1/computers", headers=admin_headers).json()["computers"] == []
    rejected = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation.installation_token}"},
        json={"instance_id": str(uuid4()), "name": "after-delete"},
    )
    assert rejected.status_code == 401

    # Repeating deletion retains its durable pending result.
    repeated = client.delete(f"/api/v1/computers/{installation_id}", headers=admin_headers)
    assert repeated.status_code == 202
    assert repeated.json()["cleanup_job_id"] == deleted.json()["cleanup_job_id"]
