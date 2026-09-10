from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "build_linux_appimage.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _run_builder(tmp_path: Path, statuses: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    cache = tmp_path / "cache" / "tauri"
    cache.mkdir(parents=True)
    tool = cache / "linuxdeploy-x86_64.AppImage"
    tool.write_bytes(b"cached-linuxdeploy-test-fixture")
    deb = tmp_path / "target" / "release" / "bundle" / "deb" / "TermFlow.deb"
    deb.parent.mkdir(parents=True)
    deb.write_bytes(b"preserved-deb")
    cargo_output = tmp_path / "target" / "release" / "deps" / "termflow"
    cargo_output.parent.mkdir(parents=True)
    cargo_output.write_bytes(b"preserved-cargo-output")

    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
set -euo pipefail
count_file="$TEST_STATE/count"
count=0
if [[ -f "$count_file" ]]; then
  count="$(<"$count_file")"
fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
printf '%s\\n' "$*" >> "$TEST_STATE/npm-args"
if [[ -e "$TERMFLOW_APPIMAGE_OUTPUT_DIR/partial" ]]; then
  printf 'stale-output-seen\\n' >> "$TEST_STATE/stale-output"
fi
mkdir -p "$TERMFLOW_APPIMAGE_OUTPUT_DIR"
printf 'partial-%s' "$count" > "$TERMFLOW_APPIMAGE_OUTPUT_DIR/partial"
IFS=',' read -r -a values <<< "$TEST_NPM_STATUSES"
index=$((count - 1))
if (( index >= ${#values[@]} )); then
  index=$((${#values[@]} - 1))
fi
exit "${values[$index]}"
""",
    )
    _write_executable(
        fake_bin / "sleep",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$1" >> "$TEST_STATE/sleeps"
""",
    )

    output_dir = tmp_path / "target" / "release" / "bundle" / "appimage"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TEST_STATE": str(state),
            "TEST_NPM_STATUSES": statuses,
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "TERMFLOW_APPIMAGE_OUTPUT_DIR": str(output_dir),
            "TERMFLOW_APPIMAGE_BUILD_ATTEMPTS": "3",
            "TERMFLOW_APPIMAGE_RETRY_DELAY_SECONDS": "10",
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, state


def test_retries_failed_appimage_build_then_succeeds(tmp_path: Path) -> None:
    result, state = _run_builder(tmp_path, "7,0")

    assert result.returncode == 0, result.stderr
    assert (state / "count").read_text(encoding="utf-8") == "2"
    assert (state / "sleeps").read_text(encoding="utf-8").splitlines() == ["10"]
    npm_args = (state / "npm-args").read_text(encoding="utf-8").splitlines()
    assert npm_args == [
        "run tauri:build --workspace @termflow/tauri-client -- "
        "--bundles appimage --ci --verbose",
    ] * 2
    assert "attempt 1/3 failed with exit status 7" in result.stderr
    assert "linuxdeploy-x86_64.AppImage" in result.stderr
    expected_hash = hashlib.sha256(b"cached-linuxdeploy-test-fixture").hexdigest()
    assert expected_hash in result.stderr
    assert "partial-1" not in result.stderr
    assert not (state / "stale-output").exists()
    assert (tmp_path / "cache" / "tauri" / "linuxdeploy-x86_64.AppImage").exists()
    assert (tmp_path / "target" / "release" / "bundle" / "deb" / "TermFlow.deb").exists()
    assert (tmp_path / "target" / "release" / "deps" / "termflow").exists()


def test_returns_final_real_status_after_three_failures(tmp_path: Path) -> None:
    result, state = _run_builder(tmp_path, "7,8,9")

    assert result.returncode == 9
    assert (state / "count").read_text(encoding="utf-8") == "3"
    assert (state / "sleeps").read_text(encoding="utf-8").splitlines() == ["10", "10"]
    assert "attempt 3/3 failed with exit status 9" in result.stderr
    assert "exhausted 3 AppImage build attempts" in result.stderr
    assert not (tmp_path / "target" / "release" / "bundle" / "appimage").exists()
    assert (tmp_path / "target" / "release" / "bundle" / "deb" / "TermFlow.deb").exists()
    assert (tmp_path / "target" / "release" / "deps" / "termflow").exists()


def test_rejects_invalid_retry_configuration_before_running_npm(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["TERMFLOW_APPIMAGE_BUILD_ATTEMPTS"] = "0"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "positive integer" in result.stderr


def test_rejects_unsafe_cleanup_directory_before_running_npm(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["TERMFLOW_APPIMAGE_OUTPUT_DIR"] = "/"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must end with /target/release/bundle/appimage" in result.stderr


def test_script_never_dumps_the_environment() -> None:
    contents = SCRIPT.read_text(encoding="utf-8")

    assert "set -x" not in contents
    assert "printenv" not in contents
    assert " env" not in contents
