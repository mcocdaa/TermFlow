from collections.abc import Callable, Iterator
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


class ProvisionedComputer(NamedTuple):
    installation_id: UUID
    installation_token: str
    response: dict[str, object]


class ProvisionedTerm(NamedTuple):
    computer: ProvisionedComputer
    instance_id: UUID
    instance_token: str
    response: dict[str, object]


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
) -> Callable[..., ProvisionedComputer]:
    def provision(
        *,
        display_name: str | None = None,
        hostname: str | None = None,
        platform: str | None = None,
        client_version: str | None = None,
    ) -> ProvisionedComputer:
        enrollment = client.post(
            "/api/v1/enrollment-tokens",
            headers=admin_headers,
            json={"display_name": display_name} if display_name is not None else None,
        )
        enrollment.raise_for_status()
        metadata = {
            "hostname": hostname,
            "platform": platform,
            "client_version": client_version,
        }
        install_payload: dict[str, object] = {
            "enrollment_token": enrollment.json()["token"],
            **{key: value for key, value in metadata.items() if value is not None},
        }
        installed = client.post("/api/v1/installations/enroll", json=install_payload)
        installed.raise_for_status()
        response = installed.json()
        return ProvisionedComputer(
            installation_id=UUID(str(response["installation_id"])),
            installation_token=str(response["installation_token"]),
            response=response,
        )

    return provision


@pytest.fixture
def provision_term(
    client: TestClient,
    provision_computer: Callable[..., ProvisionedComputer],
) -> Callable[..., ProvisionedTerm]:
    def provision(
        *,
        computer: ProvisionedComputer | None = None,
        instance_id: UUID | None = None,
        name: str = "term",
        hostname: str | None = None,
        platform: str | None = None,
        client_version: str | None = None,
    ) -> ProvisionedTerm:
        owner = computer or provision_computer(
            hostname=hostname,
            platform=platform,
            client_version=client_version,
        )
        term_id = instance_id or uuid4()
        registered = client.post(
            "/api/v1/instances/register",
            headers={"Authorization": f"Bearer {owner.installation_token}"},
            json={"instance_id": str(term_id), "name": name},
        )
        registered.raise_for_status()
        response = registered.json()
        return ProvisionedTerm(
            computer=owner,
            instance_id=term_id,
            instance_token=str(response["instance_token"]),
            response=response,
        )

    return provision
