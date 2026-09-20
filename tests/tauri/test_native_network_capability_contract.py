import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = ROOT / "apps/clients/tauri/src-tauri/capabilities"


def test_native_webview_network_capabilities_are_removed_and_opener_allowed() -> None:
    for capability_name in ("default.json", "mobile.json"):
        capability = json.loads((CAPABILITIES / capability_name).read_text())
        permissions = capability["permissions"]

        assert "websocket:default" not in permissions
        assert not any(
            isinstance(permission, dict) and permission.get("identifier") == "http:default"
            for permission in permissions
        )
        assert "opener:default" in permissions
