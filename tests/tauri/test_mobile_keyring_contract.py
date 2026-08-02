import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "apps/clients/tauri/src-tauri/Cargo.toml"


def test_mobile_targets_use_explicit_native_stores() -> None:
    manifest = tomllib.loads(MANIFEST.read_text())
    dependencies = manifest["dependencies"]
    assert "keyring" not in dependencies

    targets = manifest["target"]
    desktop = targets['cfg(not(any(target_os = "ios", target_os = "android")))'][
        "dependencies"
    ]
    ios = targets['cfg(target_os = "ios")']["dependencies"]
    android = targets['cfg(target_os = "android")']["dependencies"]

    assert desktop["keyring"]["version"] == "=4.1.5"
    assert ios["keyring-core"]["version"] == "=1.0.0"
    assert ios["apple-native-keyring-store"]["features"] == ["protected"]
    assert android["keyring-core"]["version"] == "=1.0.0"
    assert android["android-native-keyring-store"]["version"] == "=1.0.0"


def test_ios_feature_tree_enables_protected_store() -> None:
    result = subprocess.run(
        [
            "cargo",
            "tree",
            "--manifest-path",
            str(MANIFEST),
            "--target",
            "aarch64-apple-ios-sim",
            "-e",
            "features",
            "-i",
            "apple-native-keyring-store",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'apple-native-keyring-store feature "protected"' in result.stdout


def test_mobile_store_is_initialized_before_entry_creation() -> None:
    source = (ROOT / "apps/clients/tauri/src-tauri/src/auth.rs").read_text()
    assert "fn initialize_mobile_keyring" in source
    assert "initialize_mobile_keyring()?;" in source
    assert source.index("initialize_mobile_keyring()?;") < source.index(
        "KeyringEntry::new(KEYRING_SERVICE"
    )
