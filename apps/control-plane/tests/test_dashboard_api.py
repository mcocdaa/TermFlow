import time
from uuid import uuid4

from termflow_control_plane.auth.tokens import hash_token
from termflow_protocol import (
    MessageType,
    PaneSnapshot,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)


def _provision(client, admin_headers):
    enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers).json()["token"]
    installation = client.post(
        "/api/v1/installations/enroll",
        json={
            "enrollment_token": enrollment,
            "hostname": "dashboard-host",
            "platform": "Linux",
            "client_version": "0.1.0",
        },
    ).json()
    instance_id = uuid4()
    registration = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation['installation_token']}"},
        json={"instance_id": str(instance_id), "name": "old-name"},
    ).json()
    return installation, instance_id, registration["instance_token"]


def _topology(name: str) -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name=name,
        revision=2,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=40,
                        height=24,
                        left=0,
                        top=0,
                        current_command="python",
                        active=True,
                        dead=False,
                    ),
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=1,
                        title="logs",
                        width=40,
                        height=24,
                        left=40,
                        top=0,
                        current_command="tail",
                        active=False,
                        dead=False,
                    ),
                ],
            )
        ],
    )


def test_dashboard_groups_terms_and_reports_live_metrics(client, admin_headers) -> None:
    installation, instance_id, instance_token = _provision(client, admin_headers)
    client.portal.call(
        client.app.state.repositories.audit.record,
        "pane.input",
        instance_id,
        "%0",
        3,
        "ok",
        None,
    )
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.TOPOLOGY_SNAPSHOT,
                instance_id=instance_id,
                payload=TopologySnapshotPayload(topology=_topology("local-name")).model_dump(
                    mode="json"
                ),
            ).model_dump_json()
        )
        for _ in range(100):
            stored = client.portal.call(client.app.state.repositories.instances.get, instance_id)
            if stored is not None and stored.name == "local-name":
                break
            time.sleep(0.01)

        response = client.get("/api/v1/dashboard", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["metrics"] == {
            "online_terms": 1,
            "total_terms": 1,
            "active_panes": 1,
            "interactions_24h": 1,
            "computers": 1,
        }
        computer = body["computers"][0]
        assert computer["installation_id"] == installation["installation_id"]
        assert computer["online"] is True
        term = computer["terms"][0]
        assert term["name"] == "local-name"
        assert term["window_count"] == 1
        assert term["pane_count"] == 2
        assert term["current_command"] == "python"

    offline = client.get("/api/v1/dashboard", headers=admin_headers).json()
    assert offline["computers"][0]["terms"][0]["name"] == "local-name"
    assert offline["computers"][0]["terms"][0]["online"] is False
    assert hash_token("unused") not in repr(offline)
