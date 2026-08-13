# Linux AppImage Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Linux packaging job preserve the successful DEB result while giving AppImage's runtime-downloaded `linuxdeploy` toolchain bounded retries and actionable, non-sensitive failure evidence.

**Architecture:** Split the current combined Tauri Linux bundle command into a one-shot DEB step and a dedicated AppImage wrapper. The wrapper invokes Tauri with verbose diagnostics, retries at most three times with a fixed ten-second delay, reports only cached tool filename/size/SHA-256 after each failure, removes only the incomplete AppImage output directory, and returns the final real failure status. Workflow artifact requirements remain one DEB and one AppImage.

**Tech Stack:** Bash, Tauri CLI 2, GitHub Actions YAML, Python `pytest` contract tests

---

### Task 1: Specify the AppImage wrapper contract with failing tests

**Files:**
- Create: `tests/release/test_linux_appimage_builder.py`
- Create later: `scripts/release/build_linux_appimage.sh`

- [ ] **Step 1: Add tests for retry success, final failure, scoped cleanup, and safe diagnostics**

Create `tests/release/test_linux_appimage_builder.py`:

```python
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
```

- [ ] **Step 2: Run the new test and prove it fails for the missing wrapper**

Run:

```bash
python -m pytest -q tests/release/test_linux_appimage_builder.py
```

Expected: FAIL because `scripts/release/build_linux_appimage.sh` does not exist. This is the required red state.

### Task 2: Implement the bounded AppImage wrapper

**Files:**
- Create: `scripts/release/build_linux_appimage.sh`
- Test: `tests/release/test_linux_appimage_builder.py`

- [ ] **Step 1: Add the smallest wrapper that satisfies the contract**

Create `scripts/release/build_linux_appimage.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

attempts="${TERMFLOW_APPIMAGE_BUILD_ATTEMPTS:-3}"
retry_delay="${TERMFLOW_APPIMAGE_RETRY_DELAY_SECONDS:-10}"
output_dir="${TERMFLOW_APPIMAGE_OUTPUT_DIR:-apps/clients/tauri/src-tauri/target/release/bundle/appimage}"
tauri_cache="${XDG_CACHE_HOME:-${HOME}/.cache}/tauri"

if ! [[ "$attempts" =~ ^[1-9][0-9]*$ ]]; then
  echo "TERMFLOW_APPIMAGE_BUILD_ATTEMPTS must be a positive integer" >&2
  exit 2
fi
if ! [[ "$retry_delay" =~ ^[0-9]+$ ]]; then
  echo "TERMFLOW_APPIMAGE_RETRY_DELAY_SECONDS must be a non-negative integer" >&2
  exit 2
fi
case "$output_dir" in
  */target/release/bundle/appimage) ;;
  *)
    echo "TERMFLOW_APPIMAGE_OUTPUT_DIR must end with /target/release/bundle/appimage" >&2
    exit 2
    ;;
esac

report_tauri_tool_cache() {
  local tool name size digest

  if [[ ! -d "$tauri_cache" ]]; then
    echo "Tauri tool cache directory is absent" >&2
    return
  fi

  while IFS= read -r -d '' tool; do
    name="$(basename "$tool")"
    if ! size="$(stat -c '%s' "$tool")"; then
      size='unavailable'
    fi
    if digest="$(sha256sum "$tool")"; then
      digest="${digest%% *}"
    else
      digest='unavailable'
    fi
    printf 'Tauri tool cache: name=%s size=%s sha256=%s\n' \
      "$name" "$size" "$digest" >&2
  done < <(find "$tauri_cache" -maxdepth 1 -type f -print0 | sort -z)
}

status=1
for ((attempt = 1; attempt <= attempts; attempt++)); do
  echo "Building AppImage (attempt $attempt/$attempts)" >&2
  set +e
  npm run tauri:build --workspace @termflow/tauri-client -- \
    --bundles appimage --ci --verbose
  status=$?
  set -e

  if (( status == 0 )); then
    exit 0
  fi

  echo "AppImage attempt $attempt/$attempts failed with exit status $status" >&2
  report_tauri_tool_cache

  if [[ -d "$output_dir" ]]; then
    rm -rf -- "$output_dir"
  fi

  if (( attempt == attempts )); then
    break
  fi

  echo "Retrying AppImage build in ${retry_delay}s" >&2
  sleep "$retry_delay"
done

echo "AppImage build exhausted $attempts AppImage build attempts" >&2
exit "$status"
```

- [ ] **Step 2: Make the wrapper executable and run its focused tests**

Run:

```bash
chmod +x scripts/release/build_linux_appimage.sh
bash -n scripts/release/build_linux_appimage.sh
python -m pytest -q tests/release/test_linux_appimage_builder.py
```

Expected: shell syntax check succeeds and all four focused tests pass.

- [ ] **Step 3: Inspect the wrapper for its destructive and disclosure boundaries**

Run:

```bash
rg -n 'rm -rf|set -x|printenv|(^|[[:space:]])env([[:space:]]|$)' \
  scripts/release/build_linux_appimage.sh
```

Expected: the only destructive operation targets the resolved AppImage output directory; no environment dump or tracing command is present.

- [ ] **Step 4: Commit implementation plus green tests**

Run:

```bash
git add scripts/release/build_linux_appimage.sh tests/release/test_linux_appimage_builder.py
git commit -m "fix(release): retry AppImage bundling safely"
```

Expected: one implementation commit, not pushed.

### Task 3: Split DEB and AppImage workflow steps

**Files:**
- Modify: `.github/workflows/tauri-packages.yml`
- Modify: `tests/release/test_packaging_workflow_contract.py`
- Test: `tests/release/test_linux_appimage_builder.py`

- [ ] **Step 1: Change the existing workflow contract test first**

In `test_client_artifact_names_are_manual_by_default_and_tagged_when_called`, replace the combined Linux assertion:

```python
    assert "--bundles deb,appimage" in workflow
```

with:

```python
    assert "--bundles deb --ci" in workflow
    assert "scripts/release/build_linux_appimage.sh" in workflow
    assert "--bundles deb,appimage" not in workflow
```

Add this new test below it:

```python
def test_linux_builds_deb_once_before_retrying_appimage() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    deb = "npm run tauri:build --workspace @termflow/tauri-client -- --bundles deb --ci"
    appimage = "scripts/release/build_linux_appimage.sh"

    assert workflow.count(deb) == 1
    assert workflow.count(appimage) == 1
    assert workflow.index(deb) < workflow.index(appimage)
```

- [ ] **Step 2: Run the workflow contract test and prove it fails against the combined command**

Run:

```bash
python -m pytest -q \
  tests/release/test_packaging_workflow_contract.py::test_client_artifact_names_are_manual_by_default_and_tagged_when_called \
  tests/release/test_packaging_workflow_contract.py::test_linux_builds_deb_once_before_retrying_appimage
```

Expected: FAIL because the workflow still contains `--bundles deb,appimage` and does not invoke the wrapper.

- [ ] **Step 3: Replace the combined Linux build step**

In `.github/workflows/tauri-packages.yml`, replace:

```yaml
      - name: Build deb and AppImage packages
        run: npm run tauri:build --workspace @termflow/tauri-client -- --bundles deb,appimage --ci
```

with:

```yaml
      - name: Build DEB package
        run: npm run tauri:build --workspace @termflow/tauri-client -- --bundles deb --ci

      - name: Build AppImage package with bounded retries
        run: scripts/release/build_linux_appimage.sh
```

Do not change dependency installation, the artifact existence checks, artifact flattening, or the `termflow-linux-x64` upload contract.

- [ ] **Step 4: Run the focused wrapper and workflow tests**

Run:

```bash
python -m pytest -q \
  tests/release/test_linux_appimage_builder.py \
  tests/release/test_packaging_workflow_contract.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the workflow integration**

Run:

```bash
git add .github/workflows/tauri-packages.yml \
  tests/release/test_packaging_workflow_contract.py
git commit -m "ci(release): isolate deb from AppImage retries"
```

Expected: one workflow integration commit, not pushed.

### Task 4: Document the Linux packaging behavior

**Files:**
- Modify: `docs/operations.md`
- Test: `tests/release/test_packaging_workflow_contract.py`

- [ ] **Step 1: Add an operations note beside the Linux client package baseline**

Add this paragraph after the `Package C · Native Clients` artifact paragraph in `docs/operations.md`:

```markdown
The Linux job builds the DEB once, then builds AppImage separately. AppImage uses
Tauri verbose output and at most three attempts separated by 10 seconds because
Tauri downloads `linuxdeploy` and its plugins at bundle time. After a failed
attempt, CI reports only each cached tool's filename, byte size, and SHA-256,
removes only the incomplete AppImage output directory, and preserves the Cargo
build, DEB, and Tauri tool cache. The job still fails unless both one DEB and one
AppImage exist.
```

- [ ] **Step 2: Check documentation and workflow diffs**

Run:

```bash
git diff --check
git diff -- docs/operations.md .github/workflows/tauri-packages.yml
```

Expected: no whitespace errors; the documentation matches the exact implemented retry count, delay, cleanup boundary, and artifact gate.

- [ ] **Step 3: Commit the operations documentation**

Run:

```bash
git add docs/operations.md
git commit -m "docs(release): explain AppImage retry boundary"
```

Expected: documentation-only commit, not pushed.

### Task 5: Run the local release verification suite

**Files:**
- Verify: `scripts/release/build_linux_appimage.sh`
- Verify: `.github/workflows/tauri-packages.yml`
- Verify: `tests/release/`

- [ ] **Step 1: Run static shell, workflow, and release tests**

Run:

```bash
bash -n scripts/release/build_linux_appimage.sh
python -m pytest -q tests/release
git diff --check
```

Expected: all commands exit zero. Record the exact pytest pass count rather than predicting it in advance.

- [ ] **Step 2: Confirm artifact contracts and unrelated workflow paths remain intact**

Run:

```bash
rg -n --fixed-strings \
  -e 'linux-x64' \
  -e '.deb' \
  -e '.AppImage' \
  .github/workflows/tauri-packages.yml
git diff --stat 69e846c..HEAD
git status --short --branch
```

Expected: the Linux artifact name and both file requirements remain; changes are limited to the planned script, tests, workflow, and documentation. Existing unrelated worktree changes, if any, remain untouched.

- [ ] **Step 3: Review commit sequence without pushing**

Run:

```bash
git log --oneline --decorate -6
git log --format='%h %s' origin/main..HEAD
```

Expected: the design commit plus the planned wrapper/test, workflow, and documentation commits are local. Stop before any push, tag, release, or workflow dispatch unless the user explicitly authorizes delivery.

### Task 6: Validate in GitHub Actions after explicit delivery authorization

**Files:**
- External: GitHub branch and Actions run
- Modify locally: none

- [ ] **Step 1: Rebase-free freshness check immediately before an authorized push**

Run:

```bash
git fetch origin main
git status --short --branch
git rev-list --left-right --count origin/main...HEAD
```

Expected: no unexpected remote divergence and no uncommitted changes. If remote `main` advanced, stop and reconcile explicitly; do not force-push.

- [ ] **Step 2: Push only after the user explicitly requests it**

Run only with that authorization:

```bash
git push origin main
```

Expected: a fast-forward push. Report the exact new remote commit SHA.

- [ ] **Step 3: Dispatch a Linux-only packaging validation without publishing**

Capture the new run ID without relying on a pre-existing run:

```bash
set -euo pipefail
before_run="$(
  gh run list --repo mcocdaa/TermFlow \
    --workflow tauri-packages.yml --event workflow_dispatch --limit 1 \
    --json databaseId --jq '.[0].databaseId // 0'
)"
gh workflow run tauri-packages.yml \
  --repo mcocdaa/TermFlow \
  --ref main \
  -f platform=linux \
  -f version=0.1.0-rc.5 \
  -f signed_android_candidate=false

run_id=''
for _ in $(seq 1 20); do
  candidate="$(
    gh run list --repo mcocdaa/TermFlow \
      --workflow tauri-packages.yml --event workflow_dispatch --limit 1 \
      --json databaseId --jq '.[0].databaseId // 0'
  )"
  if [[ "$candidate" != "$before_run" && "$candidate" != '0' ]]; then
    run_id="$candidate"
    break
  fi
  sleep 3
done
[[ "$run_id" =~ ^[0-9]+$ ]]
printf '%s' "$run_id" > /tmp/termflow-linux-package-run-id
```

Then run:

```bash
run_id="$(</tmp/termflow-linux-package-run-id)"
[[ "$run_id" =~ ^[0-9]+$ ]]
package_dir="/tmp/termflow-linux-packages-$run_id"
gh run watch "$run_id" --repo mcocdaa/TermFlow --exit-status
gh run view "$run_id" --repo mcocdaa/TermFlow --json conclusion,headSha,jobs,url
gh run download "$run_id" --repo mcocdaa/TermFlow \
  --name termflow-linux-x64 \
  --dir "$package_dir"
find "$package_dir" -maxdepth 1 -type f \
  \( -name '*.deb' -o -name '*.AppImage' \) -print -exec sha256sum {} \;
test "$(find "$package_dir" -maxdepth 1 -type f -name '*.deb' | wc -l)" -eq 1
test "$(find "$package_dir" -maxdepth 1 -type f -name '*.AppImage' | wc -l)" -eq 1
```

Expected: the run succeeds at the pushed commit and downloads exactly one DEB and one AppImage. This is packaging validation only; it does not publish a GitHub Release and does not alter `v0.1.0-rc.5`.

- [ ] **Step 4: If AppImage still fails, preserve the evidence and stop**

Run:

```bash
run_id="$(</tmp/termflow-linux-package-run-id)"
[[ "$run_id" =~ ^[0-9]+$ ]]
gh run view "$run_id" --repo mcocdaa/TermFlow --log-failed
```

Expected on failure: logs show the real verbose `linuxdeploy` stderr, attempt numbers/final exit status, and cached tool filename/size/SHA-256. Do not add more retries, delete the cache, or switch to mutable third-party mirrors without a new root-cause review.
