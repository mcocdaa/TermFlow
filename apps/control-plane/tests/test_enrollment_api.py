def test_health_does_not_require_authentication(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_creates_and_installation_consumes_enrollment(client, admin_headers) -> None:
    issued = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
    assert issued.status_code == 201
    raw = issued.json()["token"]
    assert len(raw) >= 43

    enrolled = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": raw},
    )
    assert enrolled.status_code == 201
    assert len(enrolled.json()["installation_token"]) >= 43

    replay = client.post(
        "/api/v1/installations/enroll",
        json={"enrollment_token": raw},
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_enrollment_token"


def test_admin_route_rejects_missing_or_wrong_token(client) -> None:
    missing = client.post("/api/v1/enrollment-tokens")
    wrong = client.post(
        "/api/v1/enrollment-tokens",
        headers={"Authorization": "Bearer wrong"},
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"]["request_id"]


def test_raw_tokens_do_not_appear_in_logs(client, admin_headers, caplog) -> None:
    issued = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
    token = issued.json()["token"]
    client.post("/api/v1/installations/enroll", json={"enrollment_token": token})
    assert token not in caplog.text
