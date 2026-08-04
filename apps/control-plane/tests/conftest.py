from collections.abc import Callable, Iterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control-plane.db'}",
        allow_insecure_loopback=True,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    database = Database(settings.database_url)
    app = create_app(settings=settings, database=database)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"}


@pytest.fixture
def provision_computer(
    client: TestClient,
    admin_headers: dict[str, str],
) -> Callable[..., SimpleNamespace]:
    def provision(**metadata: str) -> SimpleNamespace:
        enrollment = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
        enrollment.raise_for_status()
        install_payload = {
            "enrollment_token": enrollment.json()["token"],
            **metadata,
        }
        installed = client.post("/api/v1/installations/enroll", json=install_payload)
        installed.raise_for_status()
        response = installed.json()
        return SimpleNamespace(
            installation_id=UUID(str(response["installation_id"])),
            installation_token=str(response["installation_token"]),
        )

    return provision


@pytest.fixture
def provision_term(
    client: TestClient,
    provision_computer: Callable[..., SimpleNamespace],
) -> Callable[..., SimpleNamespace]:
    def provision(
        *,
        computer: SimpleNamespace | None = None,
        instance_id: UUID | None = None,
        name: str = "term",
        **computer_metadata: str,
    ) -> SimpleNamespace:
        owner = computer or provision_computer(**computer_metadata)
        term_id = instance_id or uuid4()
        registered = client.post(
            "/api/v1/instances/register",
            headers={"Authorization": f"Bearer {owner.installation_token}"},
            json={"instance_id": str(term_id), "name": name},
        )
        registered.raise_for_status()
        response = registered.json()
        return SimpleNamespace(
            computer=owner,
            instance_id=term_id,
            instance_token=str(response["instance_token"]),
        )

    return provision
