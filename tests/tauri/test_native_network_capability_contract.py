import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = ROOT / "apps/clients/tauri/src-tauri/capabilities"


@pytest.mark.parametrize("capability_name", ["default.json", "mobile.json"])
def test_native_webview_network_capabilities_are_removed(capability_name: str) -> None:
    """All WebView network traffic runs through Rust-owned channels.

    The Rust side owns every HTTP request and WebSocket, so the WebView must
    not expose the http plugin or the unrestricted websocket permission set.
    """
    capability = json.loads((CAPABILITIES / capability_name).read_text())
    permissions = capability["permissions"]

    assert "websocket:default" not in permissions
    assert not any(
        isinstance(permission, dict)
        and permission.get("identifier") == "http:default"
        for permission in permissions
    )


@pytest.mark.parametrize("capability_name", ["default.json", "mobile.json"])
def test_native_opener_capability_allows_authorization_browsers(
    capability_name: str,
) -> None:
    capability = json.loads((CAPABILITIES / capability_name).read_text())
    permissions = capability["permissions"]

    assert "opener:default" in permissions
