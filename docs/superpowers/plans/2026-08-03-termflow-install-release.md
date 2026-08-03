# TermFlow Installation and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver permanent GitHub Release assets for A and C plus immutable GHCR B + Web C images, with every installation medium tested before any publication.

**Architecture:** A becomes a Linux x86_64 PyInstaller one-directory bundle installed under the current user. Its Bridge relaunches the frozen executable. B and Web C remain one image; production Compose consumes a pinned image while a separate override retains local builds. Dispatch builds test artifacts only, and a version tag creates the Release only after A, B, and C gates pass.

**Tech Stack:** Python 3.12, uv, PyInstaller, Bash, pytest, pexpect, Docker Buildx/Compose, GitHub Actions, GitHub CLI, Tauri 2.

---

### Task 1: Create the unified version gate

**Files:**
- Create: scripts/release/check_version.py
- Create: tests/release/test_check_version.py
- Modify: .github/workflows/tauri-packages.yml
- Modify: tests/deploy/test_compose_contract.py

- [ ] **Step 1: Write failing tests for all release version sources.**

~~~python
def test_checker_requires_every_product_version_file(tmp_path: Path) -> None:
    root = write_release_tree(tmp_path, version="0.1.0")
    assert check_version.load_versions(root) == {
        "package.json": "0.1.0",
        "apps/node/pyproject.toml": "0.1.0",
        "apps/node/src/termflow_node/__init__.py": "0.1.0",
        "apps/control-plane/pyproject.toml": "0.1.0",
        "packages/protocol/pyproject.toml": "0.1.0",
        "apps/clients/tauri/package.json": "0.1.0",
        "apps/clients/tauri/src-tauri/Cargo.toml": "0.1.0",
        "apps/clients/tauri/src-tauri/tauri.conf.json": "0.1.0",
    }


def test_checker_rejects_nonmatching_tag() -> None:
    assert check_version.validate_tag("0.1.0-rc.1", "v0.1.0-rc.1") == "0.1.0-rc.1"
    with pytest.raises(ValueError, match="disagree"):
        check_version.validate_tag("0.1.0", "v0.1.1")
~~~

Require the manual package workflow to have only workflow_dispatch and contents read permission.

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: uv run --frozen pytest tests/release/test_check_version.py tests/deploy/test_compose_contract.py -q

Expected: FAIL because no common checker exists and the package workflow still owns the tag trigger.

- [ ] **Step 3: Implement the checker and adopt it in package workflows.**

~~~python
def validate_tag(version: str, tag: str) -> str:
    if not re.fullmatch(V_PREFIXED_SEMVER, tag):
        raise ValueError(f"Release tag must be a v-prefixed SemVer: {tag}")
    if tag[1:] != version:
        raise ValueError(f"Release tag {tag} disagrees with configured version {version}")
    return version
~~~

Read JSON with json, TOML with tomllib, Cargo with a strict version expression, and the Node module version using ast. The command prints the common version, validates optional --tag, and writes version=<value> to GITHUB_OUTPUT. Remove the inline Node version parser from tauri-packages.yml and call this script.

- [ ] **Step 4: Re-run the focused tests.**

Run: uv run --frozen pytest tests/release/test_check_version.py tests/deploy/test_compose_contract.py -q

Expected: PASS; version drift stops before building any asset.

- [ ] **Step 5: Commit the version gate.**

~~~bash
git add scripts/release/check_version.py tests/release/test_check_version.py \
  .github/workflows/tauri-packages.yml tests/deploy/test_compose_contract.py
git commit -m "build: validate unified release versions"
~~~

### Task 2: Make A freeze-safe and buildable

**Files:**
- Modify: apps/node/src/termflow_node/cli.py
- Modify: apps/node/src/termflow_node/instances/manager.py
- Modify: apps/node/pyproject.toml
- Modify: uv.lock
- Create: scripts/release/build_node_bundle.sh
- Modify: apps/node/tests/test_cli_lifecycle.py
- Modify: apps/node/tests/test_instance_identity.py

- [ ] **Step 1: Write failing frozen runtime tests.**

~~~python
def test_version_option_prints_node_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_frozen_bridge_reexecutes_current_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(manager.sys, "frozen", True, raising=False)
    monkeypatch.setattr(manager.sys, "executable", "/opt/termflow/termflow")
    popen = Mock()
    monkeypatch.setattr(manager.subprocess, "Popen", popen)
    manager.launch_bridge(record(tmp_path))
    assert popen.call_args.args[0][:2] == ["/opt/termflow/termflow", "_bridge"]
~~~

- [ ] **Step 2: Run Node tests and verify failure.**

Run: uv run --frozen --package termflow-node pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_instance_identity.py -q

Expected: FAIL because the CLI has no version option and every Bridge uses python -m termflow_node.

- [ ] **Step 3: Implement the executable boundary and bundle script.**

~~~python
def bridge_argv(instance_id: UUID) -> list[str]:
    suffix = ["_bridge", "--instance-id", str(instance_id)]
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *suffix]
    return [sys.executable, "-m", "termflow_node", *suffix]
~~~

Use bridge_argv in launch_bridge. Add an eager --version callback. Add pyinstaller >=6.12,<7 to the Node development group and regenerate uv.lock.

~~~bash
uv run --frozen --package termflow-node pyinstaller --noconfirm --clean --onedir \
  --name termflow --paths "$REPOSITORY_ROOT/apps/node/src" \
  "$REPOSITORY_ROOT/apps/node/src/termflow_node/__main__.py"
mkdir -p "$STAGING/termflow-node-linux-x86_64"
cp -a "$BUILD_ROOT/termflow" "$STAGING/termflow-node-linux-x86_64/termflow"
printf '%s\n' "$VERSION" > "$STAGING/termflow-node-linux-x86_64/VERSION"
tar -C "$STAGING" -czf "$OUTPUT/termflow-node-linux-x86_64.tar.gz" termflow-node-linux-x86_64
~~~

build_node_bundle.sh accepts only a v-prefixed SemVer on Linux x86_64, uses only explicit output subdirectories, and verifies termflow --version before creating the tarball.

- [ ] **Step 4: Re-run frozen/runtime build gates.**

Run: uv lock && uv sync --frozen --all-packages && uv run --frozen --package termflow-node pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_instance_identity.py apps/node/tests/integration/test_tmux_lifecycle.py -q && scripts/release/build_node_bundle.sh v0.1.0 /tmp/termflow-node-bundle

Expected: PASS; the bundle contains an executable that reports 0.1.0 and can launch its private Bridge.

- [ ] **Step 5: Commit freeze-safe A packaging.**

~~~bash
git add apps/node/src/termflow_node/cli.py apps/node/src/termflow_node/instances/manager.py \
  apps/node/pyproject.toml uv.lock scripts/release/build_node_bundle.sh \
  apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_instance_identity.py
git commit -m "build(node): package frozen Linux bundle"
~~~

### Task 3: Add a tag-pinned A installer and prove an installed A reaches B

**Files:**
- Create: scripts/release/install-node-template.sh
- Create: scripts/release/render_node_installer.py
- Create: tests/release/test_node_installer.py
- Modify: tests/e2e/conftest.py
- Create: tests/release/test_installed_node_e2e.py
- Create: scripts/release/verify_node_bundle.sh
- Modify: .github/workflows/ci.yml

- [ ] **Step 1: Write failing installer and installed-node tests.**

~~~python
def test_installer_checks_checksum_and_updates_only_user_prefix(tmp_path: Path) -> None:
    release_dir = make_release_dir(tmp_path, version="v0.1.0")
    result = run_installer(release_dir, tmp_path / "home")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.local/bin/termflow").resolve().is_file()


def test_bad_checksum_preserves_existing_termflow(tmp_path: Path) -> None:
    old = install_previous_version(tmp_path / "home")
    result = run_installer(make_bad_checksum_release(tmp_path), tmp_path / "home")
    assert result.returncode != 0
    assert (tmp_path / "home/.local/bin/termflow").resolve() == old
~~~

~~~python
@pytest.mark.e2e
@pytest.mark.tmux
def test_installed_node_creates_an_online_term(termflow_system) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("installed-bundle")
    assert termflow_system.wait_until_online(instance.instance_id)
~~~

- [ ] **Step 2: Run tests and verify failure.**

Run: uv run --frozen pytest tests/release/test_node_installer.py tests/release/test_installed_node_e2e.py -q

Expected: FAIL because neither a renderer nor an installed-node selector exists.

- [ ] **Step 3: Implement rendering and installation.**

~~~bash
prefix="$HOME/.local"
release_base="https://github.com/mcocdaa/TermFlow/releases/download/@TAG@"
archive="termflow-node-linux-x86_64.tar.gz"
curl --fail --location --silent --show-error "$release_base/$archive" --output "$temporary/$archive"
curl --fail --location --silent --show-error "$release_base/SHA256SUMS" --output "$temporary/SHA256SUMS"
(cd "$temporary" && grep -F "  $archive" SHA256SUMS | sha256sum --check --status)
~~~

render_node_installer.py validates a tag and substitutes only @TAG@. The shell template checks Linux, x86_64, curl, sha256sum, and tmux >= 3.2; it accepts a test-only file URL base through TERMFLOW_RELEASE_BASE_URL; extracts into a sibling temporary directory; checks VERSION and executable; then atomically moves the version directory and a temporary symlink into the user prefix. It never invokes sudo, edits shell configuration, deletes prior versions, prints secrets, or installs a system service.

- [ ] **Step 4: Make existing E2E select a real installed binary.**

~~~python
configured = os.environ.get("TERMFLOW_NODE_EXECUTABLE")
self.node_executable = Path(configured).resolve() if configured else self.repo / ".venv/bin/termflow"
if not self.node_executable.is_file() or not os.access(self.node_executable, os.X_OK):
    raise RuntimeError(f"Invalid TERMFLOW_NODE_EXECUTABLE: {self.node_executable}")
~~~

Use self.node_executable for login, new_and_detach, and run_node. verify_node_bundle.sh creates a file-based release directory, installs into a temporary HOME, and runs the focused E2E test with TERMFLOW_NODE_EXECUTABLE. Its trap may remove only its own mktemp directory.

- [ ] **Step 5: Run the installer/E2E acceptance and add ordinary CI coverage.**

Run: uv run --frozen pytest tests/release/test_node_installer.py tests/release/test_installed_node_e2e.py -q

Expected: PASS locally. Add a non-publishing ubuntu-22.04 CI job that builds the bundle, executes verify_node_bundle.sh, and has only read permissions.

- [ ] **Step 6: Commit installed-A acceptance.**

~~~bash
git add scripts/release/install-node-template.sh scripts/release/render_node_installer.py \
  tests/release/test_node_installer.py tests/e2e/conftest.py \
  tests/release/test_installed_node_e2e.py scripts/release/verify_node_bundle.sh \
  .github/workflows/ci.yml
git commit -m "test(node): verify installed Linux bundle"
~~~

### Task 4: Use a release image for production B + Web C

**Files:**
- Modify: deploy/compose.yaml
- Create: deploy/compose.dev.yaml
- Modify: deploy/env.example
- Modify: tests/deploy/test_compose_contract.py
- Create: scripts/release/verify_control_plane_release_image.sh
- Modify: .github/workflows/ci.yml

- [ ] **Step 1: Write failing Compose contracts.**

~~~python
def test_production_compose_uses_image_without_build() -> None:
    service = yaml.safe_load(Path("deploy/compose.yaml").read_text())["services"]["control-plane"]
    assert "image" in service
    assert "build" not in service


def test_development_override_owns_local_build() -> None:
    service = yaml.safe_load(Path("deploy/compose.dev.yaml").read_text())["services"]["control-plane"]
    assert service["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile.control-plane"}
~~~

- [ ] **Step 2: Run the deployment contracts and verify failure.**

Run: uv run --frozen pytest tests/deploy/test_compose_contract.py -q

Expected: FAIL because production Compose currently performs the source build.

- [ ] **Step 3: Implement production and development Compose files.**

~~~yaml
# deploy/compose.yaml
services:
  control-plane:
    image: ${TERMFLOW_IMAGE:?set TERMFLOW_IMAGE to a pinned GHCR tag}

# deploy/compose.dev.yaml
services:
  control-plane:
    build:
      context: ..
      dockerfile: deploy/Dockerfile.control-plane
~~~

Use TERMFLOW_IMAGE as the production environment value and place ghcr.io/mcocdaa/termflow-control-plane:v0.1.0 in env.example. Preserve command, loopback port, named data volume, healthcheck, and TOTP settings.

- [ ] **Step 4: Add an actual release-image smoke command.**

~~~bash
TERMFLOW_IMAGE="$1" TERMFLOW_ADMIN_TOKEN="release-test-admin-token-which-is-long-enough" \
  TERMFLOW_HOST_PORT=18076 \
  docker compose -p termflow-release-image-test -f deploy/compose.yaml up -d --wait
curl --fail --silent --show-error http://127.0.0.1:18076/healthz
docker compose -p termflow-release-image-test -f deploy/compose.yaml down --volumes
~~~

Use a trap so only this test project and test volume are removed. Add it after the existing B image build in CI.

- [ ] **Step 5: Re-run deployment gates and commit.**

Run: uv run --frozen pytest tests/deploy -q && TERMFLOW_IMAGE=termflow-control-plane:ci docker compose -f deploy/compose.yaml config --quiet

Expected: PASS; source development now requires the explicit development override.

~~~bash
git add deploy/compose.yaml deploy/compose.dev.yaml deploy/env.example \
  tests/deploy/test_compose_contract.py scripts/release/verify_control_plane_release_image.sh \
  .github/workflows/ci.yml
git commit -m "deploy: consume versioned control-plane images"
~~~

### Task 5: Publish permanent assets only after all builds pass

**Files:**
- Create: .github/workflows/release.yml
- Modify: .github/workflows/tauri-packages.yml
- Create: tests/release/test_release_workflow_contract.py
- Modify: tests/deploy/test_compose_contract.py

- [ ] **Step 1: Write failing release workflow contracts.**

~~~python
def test_release_waits_for_all_assets_before_publish() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    assert workflow[True]["push"]["tags"] == ["v*"]
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    assert "node-bundle-verify" in workflow["jobs"]["publish"]["needs"]
    assert "ios-simulator-app" in workflow["jobs"]["publish"]["needs"]


def test_release_uses_multiarch_images_and_checksum_assets() -> None:
    text = Path(".github/workflows/release.yml").read_text()
    for value in ("linux/amd64,linux/arm64", "SHA256SUMS", "gh release create", "--prerelease"):
        assert value in text
~~~

- [ ] **Step 2: Run the contracts and verify failure.**

Run: uv run --frozen pytest tests/release/test_release_workflow_contract.py tests/deploy/test_compose_contract.py -q

Expected: FAIL because current tag packaging only uploads expiring artifacts.

- [ ] **Step 3: Implement a tag-only workflow with scoped publication.**

~~~yaml
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  publish:
    needs: [validate-version, node-bundle, node-bundle-verify, control-plane-verify, windows-nsis, linux-packages, macos-packages, android-debug-apk, ios-simulator-app]
    permissions:
      contents: write
      packages: write
~~~

Copy each existing C package job into release.yml with its exact file-count checks. Add version, A bundle/verify, and B image verification jobs. Every build job uploads run-local artifacts; only publish has write permissions.

- [ ] **Step 4: Implement final image push and GitHub Release creation.**

~~~bash
docker buildx build --platform linux/amd64,linux/arm64 --push \
  --label "org.opencontainers.image.source=https://github.com/$GITHUB_REPOSITORY" \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$GITHUB_SHA" \
  --tag "ghcr.io/$GITHUB_REPOSITORY_OWNER/termflow-control-plane:$TAG" \
  --tag "ghcr.io/$GITHUB_REPOSITORY_OWNER/termflow-control-plane:sha-$GITHUB_SHA" \
  -f deploy/Dockerfile.control-plane .

sha256sum release-assets/* > release-assets/SHA256SUMS
gh release create "$TAG" release-assets/* --title "TermFlow $TAG" --generate-notes
~~~

Before this command, the publish job uses docker/setup-buildx-action version 3 and docker/login-action version 3 with registry ghcr.io, the GitHub actor as username, and the scoped GITHUB_TOKEN as password. For a stable tag, run docker buildx imagetools create with the latest tag and the exact tag as source. For a tag containing a hyphen, append --prerelease and do not update latest. Release notes label Windows unsigned and iOS Simulator-only. No server credential, signing key, or deployment action is added.

- [ ] **Step 5: Re-run workflow contracts and commit.**

Run: uv run --frozen pytest tests/release/test_release_workflow_contract.py tests/deploy/test_compose_contract.py -q

Expected: PASS; publication cannot run before every asset and installation check succeeds.

~~~bash
git add .github/workflows/release.yml .github/workflows/tauri-packages.yml \
  tests/release/test_release_workflow_contract.py tests/deploy/test_compose_contract.py
git commit -m "ci: publish versioned TermFlow releases"
~~~

### Task 6: Document permanent installation, rollback, and prove readiness

**Files:**
- Modify: README.md
- Modify: docs/operations.md
- Modify: docs/troubleshooting.md
- Modify: tests/docs/test_documentation_contract.py

- [ ] **Step 1: Write failing documentation assertions.**

~~~python
def test_docs_distinguish_test_artifacts_from_permanent_release_assets() -> None:
    operations = Path("docs/operations.md").read_text()
    for phrase in ("GitHub Release", "GHCR", "Actions artifact", "iOS Simulator",
                   "install-termflow-node.sh", "docker compose pull"):
        assert phrase in operations
~~~

- [ ] **Step 2: Run documentation tests and verify failure.**

Run: uv run --frozen pytest tests/docs/test_documentation_contract.py -q

Expected: FAIL because current docs describe source builds and short-lived native artifacts.

- [ ] **Step 3: Add exact installation and deployment guidance.**

~~~markdown
curl -fsSL https://github.com/mcocdaa/TermFlow/releases/download/vX.Y.Z/install-termflow-node.sh | bash
termflow login --server https://termflow.example.com --code '<one-time-code>'

TERMFLOW_IMAGE=ghcr.io/mcocdaa/termflow-control-plane:vX.Y.Z docker compose --env-file .env -f deploy/compose.yaml pull
TERMFLOW_IMAGE=ghcr.io/mcocdaa/termflow-control-plane:vX.Y.Z docker compose --env-file .env -f deploy/compose.yaml up -d
~~~

Explain permanent Release assets, temporary dispatch artifacts, required tmux, no A systemd service, unsigned Windows, Simulator-only iOS, and rollback by exact A tag or B image tag without deleting termflow-data.

- [ ] **Step 4: Run the complete non-publishing gate.**

Run: uv run --frozen pytest tests/release tests/deploy tests/docs apps/node/tests tests/e2e -q && scripts/verify.sh

Expected: PASS. Record unavailable Docker or non-Linux packaging limitations instead of treating skipped checks as evidence.

- [ ] **Step 5: Validate manual packages and a prospective tag.**

Run: trigger Tauri Multi-platform Packages manually with platform all, then run git status --short --branch and uv run --frozen python scripts/release/check_version.py --tag v0.1.0.

Expected: all five temporary C artifacts succeed; no GitHub Release or GHCR version image exists. Do not create or push a tag. Commit documentation only after the documentation tests pass:

~~~bash
git add README.md docs/operations.md docs/troubleshooting.md tests/docs/test_documentation_contract.py
git commit -m "docs: explain permanent installation and releases"
~~~
