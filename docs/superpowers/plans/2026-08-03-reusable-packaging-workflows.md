# Reusable Packaging Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicated release packaging with three dual-trigger reusable workflows for A, B + Web C, and native C, while preserving manual artifacts and making Tag releases fail closed.

**Architecture:** Each packaging workflow supports both `workflow_dispatch` and `workflow_call`, computes a validated packaging context from an optional `release_tag`, and owns all commands for its product. The Tag-triggered `release.yml` becomes a thin orchestrator: call A and C, call B only after both succeed, then merge artifacts, regenerate checksums, and create the GitHub Release.

**Tech Stack:** GitHub Actions reusable workflows, YAML/PyYAML contract tests, Bash, Python 3.12, uv, Docker/Buildx/GHCR, Tauri 2, npm 10, Node 22.23.2, pytest.

---

## File responsibility map

- `scripts/release/render_node_installer.py`: validate and render both release Tag and repository slug.
- `scripts/release/install-node-template.sh`: point the generated A installer at the repository that produced it.
- `scripts/release/verify_node_bundle.sh`: pass the repository slug through the real install acceptance path.
- `.github/workflows/package-node.yml`: manual/reusable A package and offline-install artifact.
- `scripts/release/archive_control_plane_image.sh`: atomically save, unload, reload, and identity-check a Docker image tar.
- `.github/workflows/package-control-plane.yml`: manual B + Web C tar and Tag-only multiarch GHCR publication.
- `.github/workflows/tauri-packages.yml`: existing manual native C workflow plus reusable Tag mode.
- `.github/workflows/release.yml`: Tag validation, reusable-workflow dependency graph, checksum generation, and GitHub Release creation only.
- `.github/workflows/ci.yml`: ordinary verification only; remove stale `TERMFLOW_IMAGE` usage.
- `tests/release/test_node_installer.py`: repository-aware and offline installer behavior.
- `tests/release/test_control_plane_image_archive.py`: Docker save/load round-trip script contract.
- `tests/release/test_packaging_workflow_contract.py`: A/B/C dual-trigger, naming, retention, permissions, and command-ownership contracts.
- `tests/release/test_release_workflow_contract.py`: thin orchestrator and fail-closed dependency contracts.
- `tests/deploy/test_compose_contract.py`: CI and B workflow delivery boundaries.
- `tests/docs/test_documentation_contract.py`: operator documentation for the three manual workflows and Tag release.
- `README.md`, `docs/operations.md`, `docs/troubleshooting.md`: manual artifacts, offline A installation, Tag publication, and remote-image boundaries.

### Task 1: Make the A installer repository-aware and offline-artifact friendly

**Files:**
- Modify: `tests/release/test_node_installer.py`
- Modify: `scripts/release/render_node_installer.py`
- Modify: `scripts/release/install-node-template.sh`
- Modify: `scripts/release/verify_node_bundle.sh`

- [ ] **Step 1: Write exact failing installer repository tests**

Change the helper and add validation coverage in `tests/release/test_node_installer.py`:

```python
def _render_installer(
    tmp_path: Path,
    *,
    repository: str = "fork-owner/TermFlow",
) -> Path:
    installer = tmp_path / "install-termflow-node.sh"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "v0.1.0",
            str(installer),
            "--repository",
            repository,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return installer


def test_installer_defaults_to_the_rendered_repository(tmp_path: Path) -> None:
    installer = _render_installer(tmp_path, repository="fork-owner/TermFlow")

    assert (
        "https://github.com/fork-owner/TermFlow/releases/download/${TAG}"
        in installer.read_text()
    )
    assert "github.com/mcocdaa/TermFlow" not in installer.read_text()


def test_renderer_rejects_an_invalid_repository_slug(tmp_path: Path) -> None:
    installer = tmp_path / "install-termflow-node.sh"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "v0.1.0",
            str(installer),
            "--repository",
            "owner/repo/extra",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "owner/repository" in result.stderr
    assert not installer.exists()
```

Keep the existing `TERMFLOW_RELEASE_BASE_URL=release.as_uri()` install tests; they prove a downloaded manual Artifact can install from adjacent files.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q tests/release/test_node_installer.py
```

Expected: FAIL because the renderer does not accept `--repository` and the template still contains the original repository.

- [ ] **Step 3: Implement explicit repository rendering**

In `scripts/release/render_node_installer.py`, add repository validation and replace both placeholders:

```python
REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def render_installer(
    tag: str,
    repository: str,
    template: Path = TEMPLATE,
) -> str:
    if not V_PREFIXED_SEMVER.fullmatch(tag):
        raise ValueError(f"Release tag must be a v-prefixed SemVer: {tag}")
    if not REPOSITORY_SLUG.fullmatch(repository):
        raise ValueError(f"Repository must be an owner/repository slug: {repository}")
    source = template.read_text()
    if source.count("@TAG@") != 1 or source.count("@REPOSITORY@") != 1:
        raise ValueError(
            f"{template}: expected exactly one @TAG@ and one @REPOSITORY@ placeholder"
        )
    return source.replace("@TAG@", tag).replace("@REPOSITORY@", repository)
```

Add `import re`, add this argument in `parse_args()`, and pass it from `main()`:

```python
parser.add_argument("--repository", required=True)
```

Change the template line to:

```bash
readonly RELEASE_BASE="${TERMFLOW_RELEASE_BASE_URL:-https://github.com/@REPOSITORY@/releases/download/${TAG}}"
```

In `scripts/release/verify_node_bundle.sh`, resolve and validate the current repository without changing its public CLI:

```bash
REPOSITORY="${GITHUB_REPOSITORY:-mcocdaa/TermFlow}"
uv run --frozen python "${SCRIPT_DIRECTORY}/render_node_installer.py" \
  "${TAG}" "${installer}" --repository "${REPOSITORY}"
```

- [ ] **Step 4: Run installer, bundle, and shell checks**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_node_installer.py \
  tests/release/test_node_bundle.py
bash -n scripts/release/install-node-template.sh scripts/release/verify_node_bundle.sh
```

Expected: all tests PASS and shell syntax exits 0.

- [ ] **Step 5: Commit the installer boundary**

```bash
git add tests/release/test_node_installer.py \
  scripts/release/render_node_installer.py \
  scripts/release/install-node-template.sh \
  scripts/release/verify_node_bundle.sh
git commit -m "fix(release): render fork-aware node installers"
```

### Task 2: Add the manual and reusable A packaging workflow

**Files:**
- Create: `.github/workflows/package-node.yml`
- Create: `tests/release/test_packaging_workflow_contract.py`

- [ ] **Step 1: Write the failing A workflow contract**

Create `tests/release/test_packaging_workflow_contract.py` with:

```python
from pathlib import Path

import yaml


NODE_WORKFLOW = Path(".github/workflows/package-node.yml")
CONTROL_PLANE_WORKFLOW = Path(".github/workflows/package-control-plane.yml")
CLIENT_WORKFLOW = Path(".github/workflows/tauri-packages.yml")


def _workflow(path: Path) -> dict[object, object]:
    return yaml.safe_load(path.read_text())


def test_node_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(NODE_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package A · Linux Node"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"] is None
    assert triggers["workflow_call"]["inputs"]["release_tag"] == {
        "description": "Validated v-prefixed release tag; empty for manual packaging",
        "required": False,
        "default": "",
        "type": "string",
    }
    assert workflow["permissions"] == {"contents": "read"}


def test_node_workflow_owns_names_retention_and_build_commands() -> None:
    text = NODE_WORKFLOW.read_text()

    for required in (
        "termflow-node-linux-x86_64",
        "termflow-${release_tag}-node-linux-x86_64",
        "retention_days=14",
        "retention_days=1",
        "scripts/release/build_node_bundle.sh",
        "scripts/release/render_node_installer.py",
        "scripts/release/verify_node_bundle.sh",
        "--repository \"$GITHUB_REPOSITORY\"",
        "actions/upload-artifact@v4",
        "release-assets/SHA256SUMS",
    ):
        assert required in text
```

- [ ] **Step 2: Run the contract and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py::test_node_workflow_is_manual_and_reusable \
  tests/release/test_packaging_workflow_contract.py::test_node_workflow_owns_names_retention_and_build_commands
```

Expected: FAIL with `FileNotFoundError` for `.github/workflows/package-node.yml`.

- [ ] **Step 3: Implement `package-node.yml`**

Create this workflow shape, retaining these exact outputs and commands:

```yaml
name: Package A · Linux Node
run-name: Package A · ${{ inputs.release_tag || 'manual' }}

on:
  workflow_dispatch:
  workflow_call:
    inputs:
      release_tag:
        description: Validated v-prefixed release tag; empty for manual packaging
        required: false
        default: ""
        type: string

permissions:
  contents: read

concurrency:
  group: package-node-${{ github.ref }}-${{ inputs.release_tag || 'manual' }}
  cancel-in-progress: false

jobs:
  prepare:
    runs-on: ubuntu-22.04
    outputs:
      tag: ${{ steps.context.outputs.tag }}
      artifact_name: ${{ steps.context.outputs.artifact_name }}
      retention_days: ${{ steps.context.outputs.retention_days }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Resolve validated package context
        id: context
        env:
          RELEASE_TAG: ${{ inputs.release_tag }}
        shell: bash
        run: |
          set -euo pipefail
          version="$(python scripts/release/check_version.py)"
          release_tag="$RELEASE_TAG"
          if [[ -n "$release_tag" ]]; then
            python scripts/release/check_version.py --tag "$release_tag" >/dev/null
            artifact_name="termflow-${release_tag}-node-linux-x86_64"
            retention_days=1
          else
            release_tag="v${version}"
            artifact_name="termflow-node-linux-x86_64"
            retention_days=14
          fi
          echo "tag=$release_tag" >> "$GITHUB_OUTPUT"
          echo "artifact_name=$artifact_name" >> "$GITHUB_OUTPUT"
          echo "retention_days=$retention_days" >> "$GITHUB_OUTPUT"

  package:
    needs: prepare
    runs-on: ubuntu-22.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install uv and tmux
        run: |
          python -m pip install --disable-pip-version-check uv==0.11.19
          sudo apt-get update
          sudo apt-get install --yes tmux
      - name: Sync locked workspace
        run: uv sync --frozen --all-packages
      - name: Build and verify the Node artifact
        env:
          PACKAGE_TAG: ${{ needs.prepare.outputs.tag }}
        run: |
          set -euo pipefail
          mkdir -p release-assets
          scripts/release/build_node_bundle.sh "$PACKAGE_TAG" release-assets
          uv run --frozen python scripts/release/render_node_installer.py \
            "$PACKAGE_TAG" release-assets/install-termflow-node.sh \
            --repository "$GITHUB_REPOSITORY"
          (cd release-assets && sha256sum termflow-node-linux-x86_64.tar.gz > SHA256SUMS)
          scripts/release/verify_node_bundle.sh "$PACKAGE_TAG"
      - uses: actions/upload-artifact@v4
        with:
          name: ${{ needs.prepare.outputs.artifact_name }}
          path: |
            release-assets/termflow-node-linux-x86_64.tar.gz
            release-assets/install-termflow-node.sh
            release-assets/SHA256SUMS
          if-no-files-found: error
          retention-days: ${{ fromJSON(needs.prepare.outputs.retention_days) }}
          compression-level: 0
```

- [ ] **Step 4: Run the A workflow contract**

Run:

```bash
uv run --no-cache --frozen pytest -q tests/release/test_packaging_workflow_contract.py
```

Expected: A tests PASS; B tests do not exist yet.

- [ ] **Step 5: Commit the A workflow**

```bash
git add .github/workflows/package-node.yml \
  tests/release/test_packaging_workflow_contract.py
git commit -m "ci(release): add reusable node packaging"
```

### Task 3: Add a tested Control Plane image archive round-trip

**Files:**
- Create: `scripts/release/archive_control_plane_image.sh`
- Create: `tests/release/test_control_plane_image_archive.py`

- [ ] **Step 1: Write failing archive-script tests with a fake Docker CLI**

Create `tests/release/test_control_plane_image_archive.py`. The fake `docker` must record every command, create the requested save file, and return configurable IDs:

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIVER = ROOT / "scripts/release/archive_control_plane_image.sh"


def _fake_docker(tmp_path: Path) -> Path:
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "image inspect")
    count="$(cat "$FAKE_DOCKER_INSPECT_COUNT")"
    count="$((count + 1))"
    printf '%s' "$count" > "$FAKE_DOCKER_INSPECT_COUNT"
    if (( count == 1 )); then printf '%s\n' "$FAKE_DOCKER_BEFORE_ID"; else printf '%s\n' "$FAKE_DOCKER_AFTER_ID"; fi
    ;;
  "save --output")
    printf 'docker image tar' > "$3"
    ;;
  "image rm"|"load --input") ;;
  *) exit 64 ;;
esac
"""
    )
    docker.chmod(0o755)
    return binary


def _run(tmp_path: Path, *, after_id: str = "sha256:same") -> subprocess.CompletedProcess[str]:
    binary = _fake_docker(tmp_path)
    log = tmp_path / "docker.log"
    count = tmp_path / "inspect-count"
    count.write_text("0")
    return subprocess.run(
        [str(ARCHIVER), "termflow:test", str(tmp_path / "termflow.tar")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_INSPECT_COUNT": str(count),
            "FAKE_DOCKER_BEFORE_ID": "sha256:same",
            "FAKE_DOCKER_AFTER_ID": after_id,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_archiver_saves_unloads_reloads_and_preserves_identity(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "termflow.tar").read_text() == "docker image tar"
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        "image inspect --format {{.Id}} termflow:test",
        f"save --output {tmp_path / 'termflow.tar.tmp'} termflow:test",
        "image rm termflow:test",
        f"load --input {tmp_path / 'termflow.tar.tmp'}",
        "image inspect --format {{.Id}} termflow:test",
    ]


def test_archiver_rejects_a_changed_reloaded_image(tmp_path: Path) -> None:
    result = _run(tmp_path, after_id="sha256:different")

    assert result.returncode == 1
    assert "image identity changed" in result.stderr
    assert not (tmp_path / "termflow.tar").exists()
```

- [ ] **Step 2: Run the archive tests and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q tests/release/test_control_plane_image_archive.py
```

Expected: FAIL because the archive script does not exist.

- [ ] **Step 3: Implement the atomic archive script**

Create `scripts/release/archive_control_plane_image.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 || -z "$1" || -z "$2" ]]; then
  echo "usage: archive_control_plane_image.sh IMAGE OUTPUT_TAR" >&2
  exit 2
fi

IMAGE="$1"
OUTPUT_TAR="$2"
TEMPORARY_TAR="${OUTPUT_TAR}.tmp"
if [[ -e "$OUTPUT_TAR" || -e "$TEMPORARY_TAR" ]]; then
  echo "refusing to replace an existing image archive: $OUTPUT_TAR" >&2
  exit 2
fi
trap 'rm -f "$TEMPORARY_TAR"' EXIT

before_id="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
docker save --output "$TEMPORARY_TAR" "$IMAGE"
docker image rm "$IMAGE" >/dev/null
docker load --input "$TEMPORARY_TAR" >/dev/null
after_id="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
if [[ "$after_id" != "$before_id" ]]; then
  echo "control-plane image identity changed after save/load" >&2
  exit 1
fi
mv "$TEMPORARY_TAR" "$OUTPUT_TAR"
```

Make it executable with `chmod +x scripts/release/archive_control_plane_image.sh`.

- [ ] **Step 4: Run archive tests and syntax checks**

Run:

```bash
uv run --no-cache --frozen pytest -q tests/release/test_control_plane_image_archive.py
bash -n scripts/release/archive_control_plane_image.sh
```

Expected: 2 tests PASS and shell syntax exits 0.

- [ ] **Step 5: Commit the archive boundary**

```bash
git add scripts/release/archive_control_plane_image.sh \
  tests/release/test_control_plane_image_archive.py
git commit -m "feat(release): verify control-plane image archives"
```

### Task 4: Add the manual and reusable B + Web C packaging workflow

**Files:**
- Create: `.github/workflows/package-control-plane.yml`
- Modify: `tests/release/test_packaging_workflow_contract.py`

- [ ] **Step 1: Add failing B workflow contracts**

Append:

```python
def test_control_plane_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(CONTROL_PLANE_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package B + Web C · Control Plane"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"] is None
    assert "release_tag" in triggers["workflow_call"]["inputs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert workflow["jobs"]["publish"]["needs"] == ["prepare", "package"]


def test_control_plane_manual_artifact_and_tag_publication_are_separated() -> None:
    text = CONTROL_PLANE_WORKFLOW.read_text()

    for required in (
        "termflow-control-plane",
        "termflow-${release_tag}-control-plane",
        "termflow-control-plane.tar",
        "scripts/build-control-plane-image.sh",
        "scripts/verify-control-plane-image.sh",
        "scripts/release/verify_control_plane_release_image.sh",
        "scripts/release/archive_control_plane_image.sh",
        "linux/amd64,linux/arm64",
        "ghcr.io/${owner}/termflow-control-plane",
        'image_tag="${RELEASE_TAG//+/_}"',
        "docker/login-action@v3",
        "docker buildx build",
    ):
        assert required in text
    assert "if: ${{ needs.prepare.outputs.release_tag != '' }}" in text
```

- [ ] **Step 2: Run the B contracts and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py::test_control_plane_workflow_is_manual_and_reusable \
  tests/release/test_packaging_workflow_contract.py::test_control_plane_manual_artifact_and_tag_publication_are_separated
```

Expected: FAIL with `FileNotFoundError` for `.github/workflows/package-control-plane.yml`.

- [ ] **Step 3: Implement the B workflow preparation and package jobs**

Create `.github/workflows/package-control-plane.yml` with this complete header and preparation job:

```yaml
name: Package B + Web C · Control Plane
run-name: Package B + Web C · ${{ inputs.release_tag || 'manual' }}

on:
  workflow_dispatch:
  workflow_call:
    inputs:
      release_tag:
        description: Validated v-prefixed release tag; empty for manual packaging
        required: false
        default: ""
        type: string

permissions:
  contents: read

concurrency:
  group: package-control-plane-${{ github.ref }}-${{ inputs.release_tag || 'manual' }}
  cancel-in-progress: false

jobs:
  prepare:
    runs-on: ubuntu-22.04
    outputs:
      release_tag: ${{ steps.context.outputs.release_tag }}
      artifact_name: ${{ steps.context.outputs.artifact_name }}
      retention_days: ${{ steps.context.outputs.retention_days }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Resolve validated package context
        id: context
        env:
          RELEASE_TAG: ${{ inputs.release_tag }}
        shell: bash
        run: |
          set -euo pipefail
          python scripts/release/check_version.py >/dev/null
          release_tag="$RELEASE_TAG"
          if [[ -n "$release_tag" ]]; then
            python scripts/release/check_version.py --tag "$release_tag" >/dev/null
            artifact_name="termflow-${release_tag}-control-plane"
            retention_days=1
          else
            artifact_name="termflow-control-plane"
            retention_days=14
          fi
          echo "release_tag=$release_tag" >> "$GITHUB_OUTPUT"
          echo "artifact_name=$artifact_name" >> "$GITHUB_OUTPUT"
          echo "retention_days=$retention_days" >> "$GITHUB_OUTPUT"
```

The exact naming branch in that job is:

```bash
if [[ -n "$release_tag" ]]; then
  python scripts/release/check_version.py --tag "$release_tag" >/dev/null
  artifact_name="termflow-${release_tag}-control-plane"
  retention_days=1
else
  artifact_name="termflow-control-plane"
  retention_days=14
fi
```

The `package` job must run these exact steps on `ubuntu-22.04`:

```yaml
  package:
    needs: prepare
    runs-on: ubuntu-22.04
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - name: Build, verify, archive, and reload the image
        env:
          LOCAL_IMAGE: termflow-control-plane:package-${{ github.run_id }}-${{ github.run_attempt }}
        run: |
          set -euo pipefail
          mkdir -p release-assets
          scripts/build-control-plane-image.sh "$LOCAL_IMAGE"
          scripts/verify-control-plane-image.sh "$LOCAL_IMAGE"
          scripts/release/verify_control_plane_release_image.sh "$LOCAL_IMAGE"
          scripts/release/archive_control_plane_image.sh \
            "$LOCAL_IMAGE" release-assets/termflow-control-plane.tar
          scripts/verify-control-plane-image.sh "$LOCAL_IMAGE"
      - uses: actions/upload-artifact@v4
        with:
          name: ${{ needs.prepare.outputs.artifact_name }}
          path: release-assets/termflow-control-plane.tar
          if-no-files-found: error
          retention-days: ${{ fromJSON(needs.prepare.outputs.retention_days) }}
          compression-level: 0
```

- [ ] **Step 4: Add the Tag-only multiarch publish job**

Add a `publish` job with:

```yaml
  publish:
    needs: [prepare, package]
    if: ${{ needs.prepare.outputs.release_tag != '' }}
    runs-on: ubuntu-22.04
    timeout-minutes: 45
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Push the validated multi-architecture image
        env:
          RELEASE_TAG: ${{ needs.prepare.outputs.release_tag }}
        shell: bash
        run: |
          set -euo pipefail
          owner="${GITHUB_REPOSITORY_OWNER,,}"
          image="ghcr.io/${owner}/termflow-control-plane"
          image_tag="${RELEASE_TAG//+/_}"
          tags=(
            --tag "${image}:${image_tag}"
            --tag "${image}:sha-${GITHUB_SHA}"
          )
          if [[ "$RELEASE_TAG" != *-* ]]; then
            tags+=(--tag "${image}:latest")
          fi
          docker buildx build --platform linux/amd64,linux/arm64 --push \
            --label "org.opencontainers.image.source=https://github.com/${GITHUB_REPOSITORY}" \
            --label "org.opencontainers.image.version=${RELEASE_TAG#v}" \
            --label "org.opencontainers.image.revision=${GITHUB_SHA}" \
            "${tags[@]}" \
            -f deploy/Dockerfile.control-plane .
```

The workflow-level permission remains `contents: read`. Only `publish` requests package write, and manual dispatch skips it because `release_tag` is empty.

Only the GHCR version tag maps SemVer build metadata `+` to Docker-compatible `_`;
Artifact names, the GitHub Release name, and the OCI version label keep the exact validated Tag.

- [ ] **Step 5: Run B workflow, archive, and deployment contracts**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_control_plane_image_archive.py \
  tests/deploy/test_control_plane_image_build.py \
  tests/deploy/test_compose_contract.py
```

Expected: all focused tests PASS.

- [ ] **Step 6: Commit the B workflow**

```bash
git add .github/workflows/package-control-plane.yml \
  tests/release/test_packaging_workflow_contract.py
git commit -m "ci(release): add reusable control-plane packaging"
```

### Task 5: Make the existing native C workflow reusable

**Files:**
- Modify: `.github/workflows/tauri-packages.yml`
- Modify: `tests/release/test_packaging_workflow_contract.py`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `tests/release/test_check_version.py`

- [ ] **Step 1: Replace manual-only contracts with dual-trigger naming contracts**

Add to `tests/release/test_packaging_workflow_contract.py`:

```python
def test_client_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(CLIENT_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package C · Native Clients"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"]["inputs"]["platform"]["default"] == "all"
    assert triggers["workflow_call"]["inputs"]["platform"] == {
        "description": "Native client platform set",
        "required": False,
        "default": "all",
        "type": "string",
    }
    assert "release_tag" in triggers["workflow_call"]["inputs"]


def test_client_artifact_names_are_manual_by_default_and_tagged_when_called() -> None:
    text = CLIENT_WORKFLOW.read_text()

    assert "artifact_prefix=termflow" in text
    assert 'artifact_prefix="termflow-${release_tag}"' in text
    for suffix in (
        "windows-x64-nsis",
        "linux-x64",
        "macos-arm64",
        "android-arm64-debug",
        "ios-simulator-aarch64",
    ):
        assert f"${{{{ needs.validate-version.outputs.artifact_prefix }}}}-{suffix}" in text
```

Replace the older manual-only assertion in `tests/release/test_check_version.py` with a
dual-trigger assertion whose name reflects that the workflow is both manual and reusable.
Update the corresponding assertion in `tests/deploy/test_compose_contract.py` from
`{"workflow_dispatch"}` to `{"workflow_dispatch", "workflow_call"`, while keeping all
five runner/toolchain and signing-boundary assertions.

- [ ] **Step 2: Run the focused contracts and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_check_version.py \
  tests/deploy/test_compose_contract.py
```

Expected: FAIL because `tauri-packages.yml` has no `workflow_call`, context outputs, or Tag-aware names.

- [ ] **Step 3: Add reusable inputs and context outputs**

Change the workflow display name and run name first:

```yaml
name: Package C · Native Clients
run-name: Package C · ${{ inputs.platform }} · ${{ inputs.release_tag || 'manual' }}
```

Keep the existing manual `platform` choice and add:

```yaml
  workflow_call:
    inputs:
      platform:
        description: Native client platform set
        required: false
        default: all
        type: string
      release_tag:
        description: Validated v-prefixed release tag; empty for manual packaging
        required: false
        default: ""
        type: string
```

Extend `validate-version.outputs`:

```yaml
      version: ${{ steps.version.outputs.version }}
      artifact_prefix: ${{ steps.version.outputs.artifact_prefix }}
      retention_days: ${{ steps.version.outputs.retention_days }}
```

Replace its shell body with:

```bash
set -euo pipefail
version="$(python scripts/release/check_version.py)"
release_tag="$RELEASE_TAG"
if [[ -n "$release_tag" ]]; then
  python scripts/release/check_version.py --tag "$release_tag" >/dev/null
  artifact_prefix="termflow-${release_tag}"
  retention_days=1
else
  artifact_prefix=termflow
  retention_days=14
fi
echo "version=$version" >> "$GITHUB_OUTPUT"
echo "artifact_prefix=$artifact_prefix" >> "$GITHUB_OUTPUT"
echo "retention_days=$retention_days" >> "$GITHUB_OUTPUT"
```

Pass `RELEASE_TAG: ${{ inputs.release_tag }}` through the step environment.

- [ ] **Step 4: Replace all five artifact names and retention values**

Use these exact expressions:

```yaml
name: ${{ needs.validate-version.outputs.artifact_prefix }}-windows-x64-nsis
name: ${{ needs.validate-version.outputs.artifact_prefix }}-linux-x64
name: ${{ needs.validate-version.outputs.artifact_prefix }}-macos-arm64
name: ${{ needs.validate-version.outputs.artifact_prefix }}-android-arm64-debug
name: ${{ needs.validate-version.outputs.artifact_prefix }}-ios-simulator-aarch64
```

Every upload uses:

```yaml
retention-days: ${{ fromJSON(needs.validate-version.outputs.retention_days) }}
```

Do not alter the platform conditions, runner versions, Tauri commands, signing boundaries, output paths, or exact-file-count checks.

- [ ] **Step 5: Run native C contracts and commit**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_check_version.py \
  tests/deploy/test_compose_contract.py
```

Expected: all focused tests PASS.

```bash
git add .github/workflows/tauri-packages.yml \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_check_version.py \
  tests/deploy/test_compose_contract.py
git commit -m "ci(release): reuse native client packaging"
```

### Task 6: Replace the Tag release with a thin reusable-workflow orchestrator

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tests/release/test_release_workflow_contract.py`

- [ ] **Step 1: Write the failing thin-orchestrator contract**

Replace `tests/release/test_release_workflow_contract.py` with these contracts:

```python
from pathlib import Path

import yaml


WORKFLOW_PATH = Path(".github/workflows/release.yml")


def test_release_calls_the_three_base_packaging_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert workflow[True]["push"]["tags"] == ["v*"]
    assert jobs["package-node"] == {
        "name": "Package A",
        "needs": "validate-version",
        "uses": "./.github/workflows/package-node.yml",
        "with": {"release_tag": "${{ github.ref_name }}"},
    }
    assert jobs["package-clients"] == {
        "name": "Package native C",
        "needs": "validate-version",
        "uses": "./.github/workflows/tauri-packages.yml",
        "with": {
            "platform": "all",
            "release_tag": "${{ github.ref_name }}",
        },
    }
    control = jobs["package-control-plane"]
    assert set(control["needs"]) == {"package-node", "package-clients"}
    assert control["uses"] == "./.github/workflows/package-control-plane.yml"
    assert control["with"] == {"release_tag": "${{ github.ref_name }}"}
    assert control["permissions"] == {"contents": "read", "packages": "write"}


def test_release_publishes_only_after_all_reusable_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    publish = workflow["jobs"]["publish"]

    assert set(publish["needs"]) == {
        "validate-version",
        "package-node",
        "package-clients",
        "package-control-plane",
    }
    assert publish["permissions"] == {"contents": "write"}


def test_release_contains_no_product_packaging_implementation() -> None:
    text = WORKFLOW_PATH.read_text()

    for forbidden in (
        "build_node_bundle.sh",
        "tauri:build",
        "android build",
        "ios build",
        "docker buildx build",
        "Dockerfile.control-plane",
        "*-setup.exe",
        "*.AppImage",
        "*.dmg",
        "*-debug.apk",
    ):
        assert forbidden not in text
    for required in (
        "actions/download-artifact@v4",
        "merge-multiple: true",
        "SHA256SUMS",
        "gh release create",
        "--prerelease",
    ):
        assert required in text
```

- [ ] **Step 2: Run the release contracts and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q tests/release/test_release_workflow_contract.py
```

Expected: FAIL because the existing release file contains all packaging jobs and Docker/Tauri commands.

- [ ] **Step 3: Replace packaging jobs with reusable workflow calls**

Keep the existing `validate-version` job. Replace the product jobs with:

```yaml
  package-node:
    name: Package A
    needs: validate-version
    uses: ./.github/workflows/package-node.yml
    with:
      release_tag: ${{ github.ref_name }}

  package-clients:
    name: Package native C
    needs: validate-version
    uses: ./.github/workflows/tauri-packages.yml
    with:
      platform: all
      release_tag: ${{ github.ref_name }}

  package-control-plane:
    name: Package B + Web C
    needs: [package-node, package-clients]
    permissions:
      contents: read
      packages: write
    uses: ./.github/workflows/package-control-plane.yml
    with:
      release_tag: ${{ github.ref_name }}
```

- [ ] **Step 4: Implement the final artifact merge and Release job**

Use:

```yaml
  publish:
    name: Publish GitHub Release
    needs:
      - validate-version
      - package-node
      - package-clients
      - package-control-plane
    runs-on: ubuntu-22.04
    timeout-minutes: 15
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          path: release-assets
          merge-multiple: true
      - name: Regenerate checksums and create the release
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          (cd release-assets && rm -f SHA256SUMS && sha256sum -- * > SHA256SUMS)
          prerelease=()
          if [[ "${GITHUB_REF_NAME}" == *-* ]]; then
            prerelease=(--prerelease)
          fi
          gh release create "${GITHUB_REF_NAME}" release-assets/* \
            --title "TermFlow ${GITHUB_REF_NAME}" \
            --generate-notes \
            --notes "Windows package is unsigned. Android uses a debug key. iOS is Simulator-only." \
            "${prerelease[@]}"
```

The checksum command runs inside `release-assets`, so the installer sees `  termflow-node-linux-x86_64.tar.gz` rather than a path-prefixed filename.

- [ ] **Step 5: Run release and packaging contracts**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/release/test_release_workflow_contract.py \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_check_version.py
```

Expected: all focused tests PASS.

- [ ] **Step 6: Commit the thin orchestrator**

```bash
git add .github/workflows/release.yml \
  tests/release/test_release_workflow_contract.py
git commit -m "refactor(release): orchestrate reusable packaging"
```

### Task 7: Align CI and operator documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `tests/docs/test_documentation_contract.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/troubleshooting.md`

- [ ] **Step 1: Write failing CI and documentation contracts**

Require the following in the existing tests:

```python
ci = Path(".github/workflows/ci.yml").read_text()
operations = Path("docs/operations.md").read_text()
troubleshooting = Path("docs/troubleshooting.md").read_text()

assert "TERMFLOW_IMAGE" not in ci
for workflow_name in (
    "Package A · Linux Node",
    "Package B + Web C · Control Plane",
    "Package C · Native Clients",
):
    assert workflow_name in operations
assert "termflow-control-plane.tar" in operations
assert "docker load" in operations
assert 'TERMFLOW_RELEASE_BASE_URL="file://$PWD"' in operations
assert "14 天" in operations
assert "Tag" in operations and "workflow_call" in operations
assert "手动 A" in troubleshooting
assert "手动 B" in troubleshooting
```

Also require `README.md` to distinguish the A Node tar from the native C Linux deb/AppImage and to state that only Tag Release pushes GHCR.

- [ ] **Step 2: Run docs/deploy tests and verify RED**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/deploy/test_compose_contract.py \
  tests/docs/test_documentation_contract.py
```

Expected: FAIL on stale `TERMFLOW_IMAGE` and missing manual A/B workflow documentation.

- [ ] **Step 3: Remove stale CI configuration and update documentation**

Change the CI Compose check from:

```bash
TERMFLOW_IMAGE=termflow-control-plane:ci docker compose -f deploy/compose.yaml config --quiet
```

to:

```bash
TERMFLOW_ADMIN_TOKEN=ci-admin-token-that-is-long-enough \
  docker compose -f deploy/compose.yaml config --quiet
```

Document these exact operator flows:

```text
Manual A -> download termflow-node-linux-x86_64 Artifact -> extract ->
TERMFLOW_RELEASE_BASE_URL="file://$PWD" ./install-termflow-node.sh

Manual B + Web C -> download termflow-control-plane Artifact -> extract ->
docker load -i termflow-control-plane.tar

Manual C -> choose all/windows/linux/macos/android/ios -> download 14-day Artifact

Tag vX.Y.Z -> release.yml calls all three base workflows -> GHCR + permanent GitHub Release
```

Preserve the warnings for unsigned Windows, Android debug key, macOS ad-hoc signing, Simulator-only iOS, Linux x86_64-only A, and `termflow-data` volume preservation.

- [ ] **Step 4: Run documentation contracts and commit**

Run:

```bash
uv run --no-cache --frozen pytest -q \
  tests/deploy/test_compose_contract.py \
  tests/docs/test_documentation_contract.py
```

Expected: all focused tests PASS.

```bash
git add .github/workflows/ci.yml \
  tests/deploy/test_compose_contract.py \
  tests/docs/test_documentation_contract.py \
  README.md docs/operations.md docs/troubleshooting.md
git commit -m "docs(release): explain reusable package workflows"
```

### Task 8: Run complete local verification and preserve remote proof boundaries

**Files:**
- No implementation files unless a verification failure exposes a defect.

- [ ] **Step 1: Run all Python tests and static checks**

Run:

```bash
uv run --no-cache --frozen pytest -q
uv run --no-cache --frozen ruff check .
uv run --no-cache --frozen mypy packages/protocol/src apps/control-plane/src apps/node/src
```

Expected: pytest completes with no failures; Ruff and mypy exit 0.

- [ ] **Step 2: Run all client tests, typechecks, and Web build under Node 22.23.2**

Run:

```bash
source /home/mcocdaa/.nvm/nvm.sh
nvm use --silent 22.23.2
npm run test:run
npm run typecheck
npm run build:web
```

Expected: all Vitest suites and typechecks PASS; Vite emits production assets. The existing chunk-size warning is non-fatal.

- [ ] **Step 3: Run shell, version, and focused workflow checks**

Run:

```bash
bash -n \
  scripts/release/install-node-template.sh \
  scripts/release/verify_node_bundle.sh \
  scripts/release/archive_control_plane_image.sh
uv run --no-cache --frozen python scripts/release/check_version.py
uv run --no-cache --frozen python scripts/release/check_version.py --tag v0.1.0
uv run --no-cache --frozen pytest -q \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_release_workflow_contract.py \
  tests/release/test_control_plane_image_archive.py
```

Expected: shell syntax exits 0, both version commands print `0.1.0`, and all workflow contracts PASS.

- [ ] **Step 4: Exercise the real B tar round-trip locally**

Use an isolated tag and temporary output directory:

```bash
package_dir="$(mktemp -d /tmp/termflow-package-verify.XXXXXX)"
scripts/build-control-plane-image.sh termflow-control-plane:package-verify
scripts/verify-control-plane-image.sh termflow-control-plane:package-verify
scripts/release/verify_control_plane_release_image.sh termflow-control-plane:package-verify
scripts/release/archive_control_plane_image.sh \
  termflow-control-plane:package-verify \
  "$package_dir/termflow-control-plane.tar"
scripts/verify-control-plane-image.sh termflow-control-plane:package-verify
test -s "$package_dir/termflow-control-plane.tar"
docker image rm termflow-control-plane:package-verify
case "$package_dir" in
  /tmp/termflow-package-verify.*) rm -rf "$package_dir" ;;
  *) echo "Refusing to remove unexpected package directory: $package_dir" >&2; exit 1 ;;
esac
```

Expected: every command exits 0, the reloaded image passes content checks, the tar is non-empty, and only the exact test image and validated `/tmp/termflow-package-verify.*` directory are removed. Do not prune unrelated images or volumes.

- [ ] **Step 5: Review the final diff and repository state**

Run:

```bash
git diff --check b16fdb4..HEAD
git status --short --branch
git log --oneline b16fdb4..HEAD
```

Expected: no whitespace errors, no uncommitted implementation changes, and the commit list matches Tasks 1-7.

- [ ] **Step 6: Record remote-only acceptance without claiming it locally**

After integration and push, manually run these workflows from `main`:

```text
Package A · Linux Node
Package B + Web C · Control Plane
Package C · Native Clients, platform=windows
```

Verify the default Artifact names, 14-day retention, downloadable contents, offline A install, B `docker load`, and Windows NSIS presence. Do not create or push `v0.1.0` as part of implementation verification: an actual Tag run publishes GHCR and a permanent GitHub Release and requires a separate explicit release decision.
