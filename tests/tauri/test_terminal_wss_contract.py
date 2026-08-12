import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "apps/clients/tauri/src-tauri/Cargo.toml"


def test_terminal_websocket_enables_public_ca_rustls() -> None:
    manifest = tomllib.loads(MANIFEST.read_text())
    dependency = manifest["dependencies"]["tokio-tungstenite"]

    assert isinstance(dependency, dict)
    assert dependency["version"] == "0.28.0"
    assert dependency["default-features"] is False
    assert set(dependency["features"]) == {
        "connect",
        "rustls-tls-webpki-roots",
    }


def test_terminal_websocket_feature_tree_contains_rustls_and_webpki_roots() -> None:
    result = subprocess.run(
        [
            "cargo",
            "tree",
            "--manifest-path",
            str(MANIFEST),
            "-e",
            "features",
            "-i",
            "tokio-tungstenite",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'tokio-tungstenite feature "rustls-tls-webpki-roots"' in result.stdout
