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


def test_client_core_has_no_platform_runtime_dependencies() -> None:
    source = "\n".join(
        path.read_text()
        for path in (ROOT / "packages/client-core/src").rglob("*.ts")
        if not path.name.endswith(".test.ts")
    )
    for forbidden in (
        "from 'vue'",
        'from "vue"',
        "window.",
        "document.",
        "localStorage",
        "new WebSocket",
        "@tauri",
        "fetch(",
        "crypto.",
        "setTimeout(",
    ):
        assert forbidden not in source


def _client_production_files() -> list[Path]:
    roots = (
        ROOT / "apps/clients/web/src",
        ROOT / "packages/client-contracts/src",
        ROOT / "packages/client-core/src",
        ROOT / "packages/client-ui/src",
    )
    return [
        path
        for root in roots
        for path in root.rglob("*")
        if path.suffix in {".ts", ".vue"}
        and not path.name.endswith(".test.ts")
        and "test" not in path.relative_to(root).parts
    ]


def test_client_persistence_is_limited_to_browser_theme_preferences() -> None:
    references = [
        path.relative_to(ROOT).as_posix()
        for path in _client_production_files()
        if any(token in path.read_text().lower() for token in ("localstorage", "sessionstorage", "indexeddb"))
    ]
    assert references == ["apps/clients/web/src/adapters/browserThemePreferences.ts"]


def test_shared_client_packages_have_no_direct_network_storage_clipboard_or_native_apis() -> None:
    forbidden = (
        "navigator.",
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "fetch(",
        "websocket",
        "@tauri",
    )
    violations: list[str] = []
    for path in _client_production_files():
        if "packages/client-" not in path.relative_to(ROOT).as_posix():
            continue
        source = path.read_text().lower()
        if any(token in source for token in forbidden):
            violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []


def test_web_client_is_only_a_browser_composition_root() -> None:
    web_source = ROOT / "apps/clients/web/src"
    allowed = {
        "env.d.ts",
        "main.ts",
        "router.ts",
        "runtime.ts",
        "adapters/browserCanonicalServerUrl.ts",
        "adapters/browserClipboard.ts",
        "adapters/browserClock.ts",
        "adapters/browserHttpTransport.ts",
        "adapters/browserTerminalTransport.ts",
        "adapters/browserThemePreferences.ts",
        "adapters/browserVisibility.ts",
    }
    production = {
        path.relative_to(web_source).as_posix()
        for path in web_source.rglob("*")
        if path.suffix in {".ts", ".vue"}
        and not path.name.endswith(".test.ts")
        and "test" not in path.relative_to(web_source).parts
    }
    assert production == allowed
