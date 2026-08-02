# TermFlow Windows Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a manually triggered, downloadable unsigned Windows NSIS installer for local TermFlow Tauri testing without installing the Windows build toolchain on the current host.

**Architecture:** Keep cross-platform compile gates in the existing CI workflow and add a separate `workflow_dispatch` Windows packaging workflow. A native `windows-latest` runner builds only the NSIS bundle and uploads the installer as a short-lived artifact; signing and publication remain outside this workflow.

**Tech Stack:** GitHub Actions, Windows Server runner, Node 22.23.2, Rust stable MSVC, npm workspaces, Tauri 2.11, NSIS, artifact upload v4.

---

## File responsibility map

- `.github/workflows/tauri-windows-package.yml`: manual native-Windows installer build and artifact upload.
- `tests/deploy/test_compose_contract.py`: static delivery contract proving packaging remains separate from Control Plane Docker and does not regress to `--no-bundle`.
- `docs/operations.md`: operator/developer instructions for triggering, downloading, installing, and understanding unsigned warnings.

### Task 1: Add the manually triggered Windows NSIS artifact workflow

**Files:**
- Create: `.github/workflows/tauri-windows-package.yml`
- Modify: `tests/deploy/test_compose_contract.py`

- [ ] **Step 1: Write a failing static workflow contract**

Parse the new YAML path and assert:

```python
workflow = yaml.safe_load(Path(".github/workflows/tauri-windows-package.yml").read_text())
assert "workflow_dispatch" in workflow[True]
job = workflow["jobs"]["windows-nsis"]
assert job["runs-on"] == "windows-latest"
rendered = Path(".github/workflows/tauri-windows-package.yml").read_text()
assert 'node-version: "22.23.2"' in rendered
assert "npm ci" in rendered
assert "--bundles nsis" in rendered
assert "actions/upload-artifact@v4" in rendered
assert "apps/clients/tauri/src-tauri/target/release/bundle/nsis/*-setup.exe" in rendered
assert "deploy/Dockerfile.control-plane" not in rendered
```

Use `workflow[True]` because PyYAML 1.1 may parse the key `on` as boolean true.

- [ ] **Step 2: Run the contract and verify the missing workflow fails**

```bash
uv run pytest tests/deploy/test_compose_contract.py -q
```

Expected: FAIL with `FileNotFoundError` for `.github/workflows/tauri-windows-package.yml`.

- [ ] **Step 3: Implement the native Windows packaging workflow**

Create this complete workflow shape:

```yaml
name: Tauri Windows Installer

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  windows-nsis:
    runs-on: windows-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22.23.2"
          cache: npm
      - uses: dtolnay/rust-toolchain@stable
      - uses: Swatinem/rust-cache@v2
        with:
          workspaces: apps/clients/tauri/src-tauri -> target
      - run: npm ci
      - name: Build unsigned NSIS installer
        run: npm run tauri:build --workspace @termflow/tauri-client -- --bundles nsis
      - uses: actions/upload-artifact@v4
        with:
          name: termflow-windows-installer
          path: apps/clients/tauri/src-tauri/target/release/bundle/nsis/*-setup.exe
          if-no-files-found: error
          retention-days: 7
```

- [ ] **Step 4: Run the workflow contract and YAML syntax checks**

```bash
uv run pytest tests/deploy/test_compose_contract.py -q
git diff --check
```

Expected: tests PASS and no whitespace errors.

- [ ] **Step 5: Commit the workflow**

```bash
git add .github/workflows/tauri-windows-package.yml tests/deploy/test_compose_contract.py
git commit -m "ci(tauri): add manual Windows NSIS package"
```

### Task 2: Document, validate, and exercise the packaging path

**Files:**
- Modify: `docs/operations.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Write a failing documentation contract**

Assert operations documentation contains the workflow name, `termflow-windows-installer`, `*-setup.exe`, seven-day retention, unsigned/SmartScreen warning, and the statement that the Control Plane Docker image does not contain the installer.

- [ ] **Step 2: Run the docs test and verify instructions are absent**

```bash
uv run pytest tests/docs/test_documentation_contract.py -q
```

Expected: FAIL on missing Windows packaging instructions.

- [ ] **Step 3: Add exact user instructions**

Document:

1. Push the desired commit to GitHub.
2. Open Actions → Tauri Windows Installer → Run workflow.
3. Wait for `windows-nsis` to complete.
4. Download `termflow-windows-installer` from Artifacts within seven days.
5. Extract and run `TermFlow_*-setup.exe` on Windows.
6. Treat SmartScreen “unknown publisher” as expected only for this private unsigned test artifact; do not use the workflow for public release.

Also document the local Windows alternative prerequisites and command:

```powershell
npm ci
npm run tauri:build --workspace @termflow/tauri-client -- --bundles nsis
```

- [ ] **Step 4: Run documentation and local Tauri compilation gates**

```bash
uv run pytest tests/docs/test_documentation_contract.py tests/deploy/test_compose_contract.py -q
scripts/verify-tauri.sh
```

Expected: tests and the current-host unsigned Tauri compile gates exit 0. This does not claim a Windows bundle was produced locally.

- [ ] **Step 5: Trigger the workflow after the branch is pushed and inspect the artifact**

From GitHub Actions run `Tauri Windows Installer` for the pushed commit. Require a successful `windows-nsis` job and a non-empty `termflow-windows-installer` artifact containing exactly an NSIS `*-setup.exe`; do not treat YAML validation or `--no-bundle` compilation as packaging evidence.

- [ ] **Step 6: Commit the documentation**

```bash
git add docs/operations.md tests/docs/test_documentation_contract.py
git commit -m "docs(tauri): explain Windows installer download"
```

- [ ] **Step 7: Run final repository verification**

```bash
scripts/verify.sh
```

Expected: all repository verification gates exit 0; report the external workflow artifact separately because local Linux verification cannot prove a Windows installer exists.
