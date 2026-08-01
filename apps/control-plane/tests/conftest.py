from collections.abc import Iterator

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

