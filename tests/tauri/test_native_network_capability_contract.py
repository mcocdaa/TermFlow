import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = ROOT / "apps/clients/tauri/src-tauri/capabilities"
EXPECTED_HTTP_SCOPE = {
    "https://*",
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
}


@pytest.mark.parametrize("capability_name", ["default.json", "mobile.json"])
def test_native_http_capability_allows_https_and_loopback_only(
    capability_name: str,
) -> None:
    capability = json.loads((CAPABILITIES / capability_name).read_text())
    permissions = capability["permissions"]
    http_permissions = [
        permission
        for permission in permissions
        if isinstance(permission, dict) and permission.get("identifier") == "http:default"
    ]

    assert len(http_permissions) == 1
    assert {entry["url"] for entry in http_permissions[0]["allow"]} == EXPECTED_HTTP_SCOPE
    assert "http:default" not in permissions
    assert not any(
        url.startswith("http://")
        and url
        not in {
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        }
        for url in EXPECTED_HTTP_SCOPE
    )
