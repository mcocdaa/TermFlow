# Tag-Derived Build Version Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Git Tag the authoritative version for formal TermFlow releases while supporting `TERMFLOW_BUILD_VERSION` and the fixed `0.0.1-dev.0` fallback for non-Tag builds.

**Architecture:** A small standard-library resolver chooses `Tag > TERMFLOW_BUILD_VERSION > default`; a separate materializer updates only registered TermFlow manifests and local lockfile entries in each ephemeral build checkout. Every reusable packaging workflow resolves its context once, then materializes the resolved version in every runner checkout before frozen dependency installation or product build.

**Tech Stack:** Python 3.12 standard library, Bash, GitHub Actions reusable workflows, uv lockfile, npm workspaces/package-lock v3, Cargo/Tauri, pytest, PyYAML.

---

## File structure

- Create `scripts/release/build_version.py`: pure version parsing, precedence and validation; no filesystem writes.
- Create `scripts/release/version_files.py`: structured manifest/lockfile materialization and verification.
- Create `scripts/release/prepare_version.py`: CLI that joins resolution and materialization and emits GitHub outputs.
- Modify `scripts/release/check_version.py`: compatibility checker for already-materialized product surfaces.
- Create `tests/release/test_build_version.py`: resolver and CLI precedence tests.
- Create `tests/release/test_version_materialization.py`: targeted file updates, lockfile safety and idempotence tests.
- Modify product manifests and lockfiles: replace release-looking source versions with `0.0.1-dev.0`.
- Modify `apps/control-plane/src/termflow_control_plane/__init__.py` and `app.py`: expose the materialized Control Plane version without an app-level literal.
- Modify Node release scripts and their tests: permit Tag argument, environment-only version and default version without two version implementations.
- Modify `.github/workflows/package-node.yml`, `package-control-plane.yml`, `tauri-packages.yml`, and `release.yml`: resolve inputs and materialize each checkout.
- Modify release workflow contract tests and operator documentation.

### Task 1: Pure build-version resolver

**Files:**
- Create: `scripts/release/build_version.py`
- Create: `tests/release/test_build_version.py`

- [ ] **Step 1: Write failing precedence and validation tests**

Create tests that import the new module through `importlib.util` or add `scripts/release` to `sys.path`, then assert the exact model:

```python
from build_version import DEFAULT_BUILD_VERSION, BuildVersion, resolve_build_version


def test_tag_wins_over_environment() -> None:
    resolved = resolve_build_version(
        tag="v1.2.3-rc.1",
        environment={"TERMFLOW_BUILD_VERSION": "9.9.9"},
    )
    assert resolved == BuildVersion(
        version="1.2.3-rc.1",
        tag="v1.2.3-rc.1",
        is_release=True,
        is_prerelease=True,
    )


def test_environment_wins_without_tag() -> None:
    resolved = resolve_build_version(
        tag=None,
        environment={"TERMFLOW_BUILD_VERSION": "2.3.4"},
    )
    assert resolved == BuildVersion("2.3.4", "v2.3.4", False, False)


def test_default_is_used_without_tag_or_environment() -> None:
    assert DEFAULT_BUILD_VERSION == "0.0.1-dev.0"
    assert resolve_build_version(tag=None, environment={}) == BuildVersion(
        "0.0.1-dev.0", "v0.0.1-dev.0", False, True
    )
```

Parametrize invalid inputs: `v1`, `1.2.3` as Tag, `v1.2.3-foo.1`, `latest`, whitespace,
and invalid environment values. Include accepted stable, `dev`, `alpha`, `beta`, `rc`, and build-metadata versions.

- [ ] **Step 2: Run the resolver tests and observe the missing-module failure**

Run:

```bash
uv run --offline --frozen pytest -q tests/release/test_build_version.py
```

Expected: FAIL because `scripts/release/build_version.py` does not exist.

- [ ] **Step 3: Implement the resolver with no third-party import**

Implement this public contract:

```python
DEFAULT_BUILD_VERSION = "0.0.1-dev.0"
BUILD_VERSION_ENV = "TERMFLOW_BUILD_VERSION"


@dataclass(frozen=True, slots=True)
class BuildVersion:
    version: str
    tag: str
    is_release: bool
    is_prerelease: bool


def validate_version(value: str) -> str:
    """Return a cross-ecosystem version or raise ValueError."""


def resolve_build_version(
    *,
    tag: str | None,
    environment: Mapping[str, str] = os.environ,
) -> BuildVersion:
    """Resolve Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0."""
```

Use one anchored regular expression for numeric `MAJOR.MINOR.PATCH`, optional
`dev|alpha|beta|rc.NUMBER`, and optional SemVer build metadata. Tag validation additionally
requires a leading `v`. Treat `None` and `""` as absent; reject non-empty whitespace rather
than trimming it. Construct the synthetic non-release tag as `f"v{version}"`.

- [ ] **Step 4: Run focused resolver tests**

Run:

```bash
uv run --offline --frozen pytest -q tests/release/test_build_version.py
```

Expected: all resolver tests PASS.

- [ ] **Step 5: Commit the pure resolver**

```bash
git add scripts/release/build_version.py tests/release/test_build_version.py
git commit -m "feat(release): resolve tag-derived build versions"
```

### Task 2: Structured version materialization

**Files:**
- Create: `scripts/release/version_files.py`
- Create: `tests/release/test_version_materialization.py`

- [ ] **Step 1: Write failing tests against a copied release tree**

Copy only registered manifests and locks from the repository into `tmp_path`. Record third-party
entries in `uv.lock`, `package-lock.json`, and Cargo.lock, call `materialize_version(tmp_path,
"1.4.0-rc.2")`, and assert:

```python
assert verify_materialized_version(tmp_path, "1.4.0-rc.2") == []
assert json.loads((tmp_path / "package.json").read_text())["version"] == "1.4.0-rc.2"
assert (
    json.loads((tmp_path / "apps/clients/web/package.json").read_text())
    ["dependencies"]["@termflow/client-core"]
    == "1.4.0-rc.2"
)
assert 'name = "termflow-node"\nversion = "1.4.0-rc.2"' in (
    tmp_path / "uv.lock"
).read_text()
assert 'name = "termflow-client"\nversion = "1.4.0-rc.2"' in (
    tmp_path / "apps/clients/tauri/src-tauri/Cargo.lock"
).read_text()
```

Run the materializer twice and assert the second tree digest is unchanged. Assert a fixture string
such as `client_version = "0.1.0"` outside the registry is untouched. Assert all non-TermFlow
lockfile package records are byte-for-byte unchanged.

- [ ] **Step 2: Run the materialization tests and observe failure**

Run:

```bash
uv run --offline --frozen pytest -q tests/release/test_version_materialization.py
```

Expected: FAIL because `version_files.py` and its functions are absent.

- [ ] **Step 3: Implement explicit registries and targeted writers**

Define exact registries rather than scanning arbitrary files:

```python
PYPROJECTS = (
    Path("apps/node/pyproject.toml"),
    Path("apps/control-plane/pyproject.toml"),
    Path("packages/protocol/pyproject.toml"),
)
PYTHON_VERSION_MODULES = (
    Path("apps/node/src/termflow_node/__init__.py"),
    Path("apps/control-plane/src/termflow_control_plane/__init__.py"),
)
NPM_MANIFESTS = (
    Path("package.json"),
    Path("apps/clients/web/package.json"),
    Path("apps/clients/tauri/package.json"),
    Path("packages/design-tokens/package.json"),
    Path("packages/client-contracts/package.json"),
    Path("packages/client-core/package.json"),
    Path("packages/client-ui/package.json"),
)
INTERNAL_NPM_NAMES = frozenset(
    {
        "@termflow/workspace",
        "@termflow/web-client",
        "@termflow/tauri-client",
        "@termflow/design-tokens",
        "@termflow/client-contracts",
        "@termflow/client-core",
        "@termflow/client-ui",
    }
)
```

Implement:

```python
def materialize_version(root: Path, version: str) -> None: ...
def verify_materialized_version(root: Path, expected: str) -> list[str]: ...
```

For JSON, use `json.loads`/`json.dumps(indent=2) + "\n"`; update each registered package version and
only dependency keys in `INTERNAL_NPM_NAMES`. Apply the same rule to the package-lock `packages`
mapping. For TOML, replace only the `version` field inside the named `[project]` or `[package]`
section. For `uv.lock` and Cargo.lock, replace only the version line in package blocks named
`termflow-control-plane`, `termflow-node`, `termflow-protocol`, or `termflow-client`. For Python
modules, require exactly one `__version__ = "..."` assignment before replacing it. Raise a
descriptive `ValueError` when an expected file, section, package block, or single assignment is
missing or duplicated.

- [ ] **Step 4: Run materialization tests and repository formatting checks**

Run:

```bash
uv run --offline --frozen pytest -q tests/release/test_version_materialization.py
uv run --offline --frozen ruff check scripts/release/version_files.py tests/release/test_version_materialization.py
```

Expected: tests PASS and Ruff reports no issues.

- [ ] **Step 5: Commit the materializer**

```bash
git add scripts/release/version_files.py tests/release/test_version_materialization.py
git commit -m "feat(release): materialize product versions"
```

### Task 3: CLI, checker, and development baseline

**Files:**
- Create: `scripts/release/prepare_version.py`
- Modify: `scripts/release/check_version.py`
- Modify: all registered manifests and local lockfile entries from Task 2
- Modify: `apps/control-plane/src/termflow_control_plane/__init__.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/node/tests/test_login.py`
- Modify: `tests/release/test_check_version.py`
- Modify: `tests/release/test_build_version.py`
- Create: `apps/control-plane/tests/test_app_version.py`

- [ ] **Step 1: Write failing CLI and runtime-version tests**

Add subprocess tests for:

```bash
python scripts/release/prepare_version.py --root <copy> --tag v2.0.0 --resolve-only
```

Assert stdout is `2.0.0`, files remain unchanged, and a temporary `GITHUB_OUTPUT` receives:

```text
version=2.0.0
tag=v2.0.0
is_release=true
```

Then run without Tag using `TERMFLOW_BUILD_VERSION=3.1.0`, assert the copied files are materialized,
and run with neither input to assert `0.0.1-dev.0`. Add an app test asserting FastAPI's version is
`termflow_control_plane.__version__`, not a literal:

```python
from termflow_control_plane import __version__
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


def test_app_reports_materialized_package_version(settings: Settings) -> None:
    app = create_app(settings=settings, database=Database(settings.database_url))
    assert app.version == __version__
```

Change Node enrollment expectations to import and use `termflow_node.__version__`.

- [ ] **Step 2: Run focused tests and observe failures**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_build_version.py \
  tests/release/test_check_version.py \
  apps/node/tests/test_login.py \
  apps/control-plane/tests/test_app_version.py
```

If the app test lives in another existing module, use that exact module. Expected: FAIL because the
CLI, Control Plane version export, and new baseline do not exist.

- [ ] **Step 3: Implement `prepare_version.py` orchestration**

The CLI must have these arguments and behavior:

```python
parser.add_argument("--root", type=Path, default=Path.cwd())
parser.add_argument("--tag", default="")
parser.add_argument("--resolve-only", action="store_true")

resolved = resolve_build_version(tag=args.tag or None)
if not args.resolve_only:
    materialize_version(root, resolved.version)
    errors = verify_materialized_version(root, resolved.version)
    if errors:
        raise ValueError("; ".join(errors))
print(resolved.version)
```

If `GITHUB_OUTPUT` is set, append `version`, `tag`, and lower-case `is_release`. Catch filesystem and
validation errors, print one concise message to stderr, and exit 2.

- [ ] **Step 4: Convert `check_version.py` into a post-materialization checker**

Keep its current `--root` and `--tag` interface for scripts/tests. Read all registered surfaces
through `version_files.py`; without Tag print the agreed version. With Tag, resolve it through
`build_version.py` and require the materialized version to equal the resolved Tag version. Do not use
`check_version.py --tag` in the Release preflight before materialization.

- [ ] **Step 5: Replace source release literals with the development baseline**

Run the new materializer once on the feature worktree:

```bash
TERMFLOW_BUILD_VERSION=0.0.1-dev.0 \
  uv run --offline --frozen python scripts/release/prepare_version.py
```

The command updates only registered manifests, internal dependency pins, `uv.lock`, `package-lock.json`,
Cargo.lock, Tauri config, and the two Python `__version__` assignments. Then change Control Plane app
construction to:

```python
from termflow_control_plane import __version__

app = FastAPI(
    title="TermFlow Control Plane",
    version=__version__,
    lifespan=lifespan,
)
```

and define `__version__ = "0.0.1-dev.0"` in the package initializer for later materialization.

- [ ] **Step 6: Verify locks, tests, and idempotence**

Run:

```bash
uv lock --offline --check
npm ci --ignore-scripts --offline
cargo metadata --locked --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --no-deps >/dev/null
uv run --offline --frozen pytest -q \
  tests/release/test_build_version.py \
  tests/release/test_version_materialization.py \
  tests/release/test_check_version.py \
  apps/node/tests/test_login.py \
  apps/control-plane/tests/test_app_version.py
git diff --check
```

Expected: lock checks and focused tests PASS. Run `prepare_version.py` again with the same version and
assert `git diff` is unchanged from the first run.

- [ ] **Step 7: Commit the CLI and development baseline**

```bash
git add scripts/release/prepare_version.py scripts/release/check_version.py \
  package.json package-lock.json uv.lock apps packages \
  tests/release/test_build_version.py tests/release/test_check_version.py
git commit -m "refactor(release): make source versions development-only"
```

### Task 4: A packaging uses Tag, environment, or default

**Files:**
- Modify: `scripts/release/build_node_bundle.sh`
- Modify: `scripts/release/verify_node_bundle.sh`
- Modify: `.github/workflows/package-node.yml`
- Modify: `tests/release/test_node_bundle.py`
- Modify: `tests/release/test_node_installer.py`
- Modify: `tests/release/test_packaging_workflow_contract.py`

- [ ] **Step 1: Write failing A script and workflow tests**

Add shell/subprocess tests proving both invocations resolve through the shared code:

```python
result = subprocess.run(
    [str(BUILD_SCRIPT), str(tmp_path / "output")],
    env={**os.environ, "TERMFLOW_BUILD_VERSION": "1.2.3"},
    ...,
)
```

Use a fast validation path or invalid version to avoid running PyInstaller in the negative test.
Assert an explicit Tag argument wins over a conflicting environment variable. Update workflow
contracts to require optional `version` inputs for both `workflow_dispatch` and `workflow_call`,
`TERMFLOW_BUILD_VERSION`, and a materialization step before `uv sync --frozen`.

- [ ] **Step 2: Run focused tests and observe failures**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_node_bundle.py \
  tests/release/test_node_installer.py \
  tests/release/test_packaging_workflow_contract.py
```

Expected: FAIL because A still requires a Tag positional argument and the workflow has no manual version input.

- [ ] **Step 3: Make the A build script delegate version resolution**

Support these exact forms:

```text
build_node_bundle.sh OUTPUT_DIRECTORY
build_node_bundle.sh vX.Y.Z OUTPUT_DIRECTORY
```

For the one-argument form, run `prepare_version.py` with no Tag; for the two-argument form, run it with
`--tag "$TAG"`. Capture its printed version, use `v${VERSION}` only when a synthetic Tag is needed,
and remove the script's duplicate SemVer regular expression. Keep the existing Linux/x86_64 guards,
PyInstaller build, embedded `VERSION`, and executable `--version` comparison.

- [ ] **Step 4: Update A workflow context and checkout materialization**

Declare optional `version` inputs in both triggers. Resolve context with:

```yaml
env:
  RELEASE_TAG: ${{ inputs.release_tag }}
  TERMFLOW_BUILD_VERSION: ${{ inputs.version || vars.TERMFLOW_BUILD_VERSION }}
run: python scripts/release/prepare_version.py --tag "$RELEASE_TAG" --resolve-only
```

Output `version`, `tag`, and `is_release`; choose tagged Artifact naming and one-day retention only
when `is_release == 'true'`. In the package job, set `TERMFLOW_BUILD_VERSION` to the resolved version,
run `prepare_version.py` immediately after checkout/setup Python and before `uv sync --frozen`, then
pass the resolved Tag to the existing installer renderer and verifier.

- [ ] **Step 5: Run A tests and build a version-injected bundle**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_node_bundle.py \
  tests/release/test_node_installer.py \
  tests/release/test_packaging_workflow_contract.py
bash -n scripts/release/build_node_bundle.sh scripts/release/verify_node_bundle.sh
```

When local PyInstaller dependencies are present, additionally build outside the repository, then run
the materializer with the fixed default to restore the feature checkout's registered files:

```bash
termflow_bundle_tmp="$(mktemp -d)"
TERMFLOW_BUILD_VERSION=1.2.3 scripts/release/build_node_bundle.sh "$termflow_bundle_tmp"
tar -xzf "$termflow_bundle_tmp/termflow-node-linux-x86_64.tar.gz" -C "$termflow_bundle_tmp"
"$termflow_bundle_tmp/termflow-node-linux-x86_64/termflow/termflow" --version
TERMFLOW_BUILD_VERSION=0.0.1-dev.0 \
  uv run --offline --frozen python scripts/release/prepare_version.py
git diff --check
```

Expected version: `1.2.3`.

- [ ] **Step 6: Commit A integration**

```bash
git add scripts/release/build_node_bundle.sh scripts/release/verify_node_bundle.sh \
  .github/workflows/package-node.yml tests/release
git commit -m "ci(node): derive package version from build context"
```

### Task 5: B and Web C packaging materialize the resolved version

**Files:**
- Modify: `.github/workflows/package-control-plane.yml`
- Modify: `deploy/Dockerfile.control-plane` only if a runtime build label/env is required after package metadata verification
- Modify: `tests/release/test_packaging_workflow_contract.py`
- Modify: `tests/release/test_deploy_artifacts.py` or the existing Dockerfile contract test

- [ ] **Step 1: Write failing B workflow contracts**

Require optional manual/call `version` inputs, resolver outputs, and `prepare_version.py` in both the
amd64 tar package checkout and multi-architecture publish checkout. Assert each call occurs before
`scripts/build-control-plane-image.sh` or `docker buildx build`. Assert the publish `if` uses
`is_release == 'true'`, not merely a non-empty synthetic Tag.

- [ ] **Step 2: Run the B workflow tests and observe failure**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_deploy_artifacts.py
```

Expected: FAIL because B has no version input or per-checkout materialization.

- [ ] **Step 3: Update B prepare, package, and publish jobs**

Map inputs with the same resolver environment as A. Add `version`, `tag`, and `is_release` outputs.
Use the original/synthetic tag only for naming; only `is_release` enables GHCR. In both build jobs:

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"
- name: Materialize build version
  env:
    TERMFLOW_BUILD_VERSION: ${{ needs.prepare.outputs.version }}
  run: python scripts/release/prepare_version.py
```

Keep the tar image name, image content verification, health smoke, save/load round-trip, multi-arch
platforms, `+` to `_` Docker-tag mapping, stable-only `latest`, and permission boundaries unchanged.
Set `org.opencontainers.image.version` from the resolved version output.

- [ ] **Step 4: Run B contracts and the existing fake-Docker archive test**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_control_plane_image_archive.py \
  tests/release/test_deploy_artifacts.py
```

Expected: PASS. Do not pull base images solely for this task; a real Docker build remains an optional
environment-dependent verification unless all bases are already available.

- [ ] **Step 5: Commit B integration**

```bash
git add .github/workflows/package-control-plane.yml deploy/Dockerfile.control-plane tests/release
git commit -m "ci(control-plane): inject resolved build version"
```

### Task 6: Native C packaging materializes every platform checkout

**Files:**
- Modify: `.github/workflows/tauri-packages.yml`
- Modify: `tests/release/test_packaging_workflow_contract.py`

- [ ] **Step 1: Write failing native workflow contracts**

Require optional `version` inputs in dispatch and call. Assert `validate-version` resolves Tag/env/default
and exposes `version`, `tag`, and `is_release`. For each job key below, require a Python setup and
materialization step before `npm ci`:

```python
for job_name in (
    "windows-nsis",
    "linux-packages",
    "macos-packages",
    "android-debug-apk",
    "ios-simulator-app",
):
    steps = workflow["jobs"][job_name]["steps"]
    # assert prepare_version.py precedes the npm-ci step
```

- [ ] **Step 2: Run native contracts and observe failure**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py
```

Expected: FAIL because no platform job materializes a build version.

- [ ] **Step 3: Update validation and all five build jobs**

Resolve inputs exactly as A/B. For every platform checkout, install Python 3.12, set
`TERMFLOW_BUILD_VERSION` from `needs.validate-version.outputs.version`, and run
`prepare_version.py` before `npm ci`, Rust cache calculation that reads the manifest, or Tauri project
generation. Keep the current Node, Rust, Java, Android, Xcode, keyring features, bundle formats, debug
signing and expected-file-count checks unchanged.

- [ ] **Step 4: Run native workflow and version-materialization contracts**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_version_materialization.py
npm ci --ignore-scripts --offline
npm run typecheck
cargo check --locked --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
```

Expected: workflow tests, npm lock install, TypeScript checks, and Rust check PASS.

- [ ] **Step 5: Commit native integration**

```bash
git add .github/workflows/tauri-packages.yml tests/release
git commit -m "ci(clients): materialize tag-derived versions"
```

### Task 7: Release orchestration, documentation, and full verification

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tests/release/test_release_workflow_contract.py`
- Modify: `tests/docs/test_documentation_contract.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/troubleshooting.md`

- [ ] **Step 1: Write failing Release and documentation contracts**

Assert the Release validation job calls:

```bash
python scripts/release/prepare_version.py --tag "$GITHUB_REF_NAME" --resolve-only
```

and no longer calls the source-equality checker. Preserve exact reusable workflow calls, Tag inputs,
failure dependencies and final Release permissions. Add documentation assertions for
`TERMFLOW_BUILD_VERSION`, `0.0.1-dev.0`, and `Tag > environment > default`.

- [ ] **Step 2: Run the Release contracts and observe failure**

Run:

```bash
uv run --offline --frozen pytest -q \
  tests/release/test_release_workflow_contract.py \
  tests/docs/test_documentation_contract.py
```

Expected: FAIL on old checker text and missing environment/default documentation.

- [ ] **Step 3: Update the thin Release orchestrator and docs**

Change only the validation command; continue passing `github.ref_name` to A, B, and C. Document:

```text
Formal release: git tag vX.Y.Z; git push origin vX.Y.Z
Manual/local override: TERMFLOW_BUILD_VERSION=X.Y.Z
No Tag or override: 0.0.1-dev.0
Priority: Git Tag > TERMFLOW_BUILD_VERSION > default
```

State that setting `TERMFLOW_BUILD_VERSION` cannot publish GHCR or GitHub Release and cannot override a
Tag run. Remove instructions that require editing every product version before tagging.

- [ ] **Step 4: Run the focused release suite**

Run:

```bash
uv run --offline --frozen pytest -q tests/release
```

Expected: all release tests PASS.

- [ ] **Step 5: Run full repository verification**

Run fresh commands from the feature worktree:

```bash
uv lock --offline --check
uv run --offline --frozen pytest -q
uv run --offline --frozen ruff check .
uv run --offline --frozen mypy packages/protocol/src apps/control-plane/src apps/node/src
npm ci --ignore-scripts --offline
npm run test:run
npm run typecheck
npm run build:web
cargo check --locked --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
bash -n scripts/release/*.sh
git diff --check
```

Expected: all commands exit 0. Report any pre-existing web chunk warning separately; do not call a
local workflow contract a successful remote Tag publication.

- [ ] **Step 6: Commit docs and orchestration**

```bash
git add .github/workflows/release.yml tests/release README.md docs/operations.md docs/troubleshooting.md
git commit -m "docs(release): explain tag-derived build versions"
```

- [ ] **Step 7: Review the complete branch before integration**

Run:

```bash
git status --short
git log --oneline --decorate main..HEAD
git diff --stat main...HEAD
git diff --check main...HEAD
```

Verify every spec requirement maps to a passing test, no downloaded Artifact or generated build output
is tracked, and the worktree source baseline remains `0.0.1-dev.0` after all injected-version tests.

### Review amendment: platform technical versions and prerelease state

The implementation review added one blocking follow-up before integration:

- reject Unicode digits, `0.0.0`, and numeric cores outside Android/Apple bundle ranges;
- emit `is_prerelease` from the shared resolver so build metadata containing `-` is not misclassified;
- materialize Android `versionCode` and numeric-core macOS/iOS platform configs;
- inject the full logical package version into the Tauri frontend for client authorization metadata;
- retain the full SemVer for Windows/Linux and record Debian prerelease ordering as a future repository-channel constraint.
