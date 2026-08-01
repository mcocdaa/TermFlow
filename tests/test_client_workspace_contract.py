import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _manifest(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


def test_client_workspace_has_one_lock_and_fixed_dependency_direction() -> None:
    root = _manifest("package.json")
    assert root["engines"] == {"node": ">=22 <23"}
    assert root["packageManager"] == "npm@10.9.8"
    assert set(root["workspaces"]) == {
        "apps/clients/*",
        "packages/design-tokens",
        "packages/client-contracts",
        "packages/client-core",
        "packages/client-ui",
    }
    assert (ROOT / "package-lock.json").is_file()
    assert not (ROOT / "apps/clients/web/package-lock.json").exists()

    contracts = _manifest("packages/client-contracts/package.json")
    core = _manifest("packages/client-core/package.json")
    ui = _manifest("packages/client-ui/package.json")
    web = _manifest("apps/clients/web/package.json")
    assert contracts.get("dependencies", {}) == {}
    assert core["dependencies"] == {"@termflow/client-contracts": "0.1.0"}
    assert set(ui["dependencies"]) >= {
        "@termflow/client-core",
        "@termflow/design-tokens",
        "vue",
        "vue-router",
    }
    assert set(web["dependencies"]) >= {"@termflow/client-core", "@termflow/client-ui"}


def test_registry_packages_are_pinned_with_integrity() -> None:
    lock = _manifest("package-lock.json")
    workspace_paths = {
        "apps/clients/web",
        "packages/design-tokens",
        "packages/client-contracts",
        "packages/client-core",
        "packages/client-ui",
    }
    registry_packages = {
        path: metadata
        for path, metadata in lock["packages"].items()
        if path and path not in workspace_paths and not metadata.get("link", False)
    }
    missing = [
        path
        for path, metadata in registry_packages.items()
        if not metadata.get("resolved") or not metadata.get("integrity")
    ]
    assert missing == []
