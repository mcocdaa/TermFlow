import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _manifest(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


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


def test_python_dependencies_come_from_portable_public_pypi_only() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = lock["package"]
    registries = {
        package["source"]["registry"]
        for package in packages
        if "registry" in package["source"]
    }
    artifact_urls = [
        artifact["url"]
        for package in packages
        for artifact in ([package["sdist"]] if "sdist" in package else [])
        + package.get("wheels", [])
    ]
    assert registries == {"https://pypi.org/simple"}
    assert artifact_urls
    assert all(url.startswith("https://files.pythonhosted.org/") for url in artifact_urls)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    uv = pyproject.get("tool", {}).get("uv", {})
    assert uv.get("default-index") or uv.get("index-url") == "https://pypi.org/simple"
    for index in uv.get("index", []):
        assert index.get("url") == "https://pypi.org/simple"

    commands = [
        line.strip()
        for line in (ROOT / "scripts/verify.sh").read_text().splitlines()
        if line.strip().startswith("uv run ")
    ]
    assert commands
    assert all(" --frozen " in command for command in commands)


def test_client_workspace_boundaries_and_platform_abstraction() -> None:
    root = _manifest("package.json")
    workspace_version = root["version"]
    assert workspace_version == "0.2.0"
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
    assert core["dependencies"] == {"@termflow/client-contracts": workspace_version}
    assert set(ui["dependencies"]) >= {
        "@termflow/client-contracts",
        "@termflow/client-core",
        "@termflow/design-tokens",
        "vue",
        "vue-router",
    }
    assert set(web["dependencies"]) == {
        "@termflow/client-core",
        "@termflow/client-ui",
        "vue",
        "vue-router",
    }
    assert ui["exports"]["./styles"] == "./src/styles/index.css"

    lock = _manifest("package-lock.json")
    workspace_paths = {
        "apps/clients/tauri",
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

    core_source = "\n".join(
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
        assert forbidden not in core_source

    storage_references = [
        path.relative_to(ROOT).as_posix()
        for path in _client_production_files()
        if any(
            token in path.read_text().lower()
            for token in ("localstorage", "sessionstorage", "indexeddb")
        )
    ]
    assert storage_references == [
        "apps/clients/web/src/adapters/browserAgentCursorStore.ts",
        "apps/clients/web/src/adapters/browserThemePreferences.ts",
    ]

    forbidden = (
        "navigator.",
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "fetch(",
        "websocket",
        "@tauri",
    )
    for path in _client_production_files():
        if "packages/client-" not in path.relative_to(ROOT).as_posix():
            continue
        source = path.read_text().lower()
        assert not any(token in source for token in forbidden), path

    web_source = ROOT / "apps/clients/web/src"
    allowed = {
        "env.d.ts",
        "main.ts",
        "router.ts",
        "runtime.ts",
        "adapters/browserAgentCursorStore.ts",
        "adapters/browserAgentStreamTransport.ts",
        "adapters/browserCanonicalServerUrl.ts",
        "adapters/browserClipboard.ts",
        "adapters/browserClock.ts",
        "adapters/browserHttpTransport.ts",
        "adapters/browserPlatform.ts",
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