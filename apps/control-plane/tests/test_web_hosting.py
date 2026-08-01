from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


def _client(tmp_path: Path, static_dir: Path) -> TestClient:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'web.db'}",
        static_dir=static_dir,
    )
    return TestClient(create_app(settings=settings, database=Database(settings.database_url)))


def test_hosts_index_assets_and_spa_routes_without_swallowing_apis(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets = static_dir / "assets"
    assets.mkdir(parents=True)
    marker = "TERMFLOW_WEB_INDEX"
    (static_dir / "index.html").write_text(
        f"<!doctype html><html><body>{marker}</body></html>",
        encoding="utf-8",
    )
    asset_name = "index-Ab12_cd3.js"
    (assets / asset_name).write_text("window.termflowLoaded = true", encoding="utf-8")

    with _client(tmp_path, static_dir) as client:
        for path in ("/", "/computers", f"/terms/{uuid4()}", "/future-client-route"):
            response = client.get(path)
            assert response.status_code == 200
            assert marker in response.text
            assert response.headers["cache-control"] == "no-cache"

        asset = client.get(f"/assets/{asset_name}")
        assert asset.status_code == 200
        assert asset.text == "window.termflowLoaded = true"
        assert "immutable" in asset.headers["cache-control"]

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        unknown_api = client.get("/api/v1/not-a-real-endpoint")
        assert unknown_api.status_code == 404
        assert unknown_api.headers["content-type"].startswith("application/json")
        assert marker not in unknown_api.text

        missing_asset = client.get("/assets/missing-Ab12_cd3.js")
        assert missing_asset.status_code == 404
        assert marker not in missing_asset.text


def test_web_responses_have_baseline_security_headers(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>TermFlow</title>")

    with _client(tmp_path, static_dir) as client:
        response = client.get("/")

    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_source_only_run_keeps_api_available_when_web_build_is_absent(tmp_path: Path) -> None:
    missing = tmp_path / "not-built"
    with _client(tmp_path, missing) as client:
        root = client.get("/")
        health = client.get("/healthz")

    assert root.status_code == 503
    assert "Web C assets are unavailable" in root.text
    assert health.status_code == 200
