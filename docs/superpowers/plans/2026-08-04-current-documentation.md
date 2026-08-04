# TermFlow Current Documentation Alignment Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository's user-facing Markdown describe the current TermFlow runtime, local deployment, and GitHub Actions packaging behavior exactly as implemented.

**Architecture:** Treat the checked-in workflows, Compose files, release scripts, and package manifests as the source of truth. Keep `docs/superpowers/specs/` and completed plans as historical engineering records, add an index that identifies their status, and move current operator instructions into README and focused documents.

**Tech Stack:** Markdown, GitHub Actions YAML, Docker Compose, Python/uv, npm workspaces, Tauri 2.

---

### Task 1: Establish the current documentation map

**Files:**
- Create: `docs/github-actions.md`
- Create: `docs/superpowers/README.md`
- Modify: `README.md`

- [x] **Step 1: Document the three reusable packaging workflows and the tag orchestrator.**

  Record the exact workflow filenames, `workflow_dispatch` inputs, artifact names, retention rules, and the `release.yml` dependency order. Explicitly state that manual runs create artifacts only, while a validated `v*` tag calls the same reusable workflows and creates a Release only after all package jobs succeed.

- [x] **Step 2: Add a historical-record index.**

  Explain that dated specs/plans record design and implementation history, not guaranteed current commands; link readers to the current operator documents and list the authoritative source files for runtime and CI behavior.

- [x] **Step 3: Link the new action guide from the root README.**

  Keep README as the shortest onboarding path: release installation, source Compose startup, local development, and links to the action guide and operator documents.

### Task 2: Correct deployment, release, and version instructions

**Files:**
- Modify: `docs/operations.md`
- Modify: `docs/troubleshooting.md`
- Modify: `.env.example`

- [x] **Step 1: Reconcile Compose instructions with `deploy/compose.yaml`.**

  Use `docker compose --env-file .env -f deploy/compose.yaml ...` from the repository root, explain loopback binding, external TLS/reverse proxy ownership, the single-worker and metadata-volume boundary, and state that the default Compose file always builds the current checkout.

- [x] **Step 2: Reconcile release instructions with the release scripts.**

  Explain `vMAJOR.MINOR.PATCH[-channel.N][+metadata]`, `TERMFLOW_BUILD_VERSION`, the `0.0.1-dev.0` fallback, and that tag versions are materialized in temporary CI checkouts rather than committed back. Keep exact artifact/platform/signing limitations.

- [x] **Step 3: Add actionable failure paths.**

  Cover failed tag publication (no GitHub Release/GHCR publication), artifact download/retention, Docker registry failures, native runner requirements, and preserving `termflow-data` during recovery.

### Task 3: Align client, API, and security guides

**Files:**
- Modify: `apps/clients/README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/protocol.md`
- Modify: `docs/security.md`
- Modify: `docs/api-examples.md`
- Modify: `docs/web-client.md`

- [x] **Step 1: Describe current client ownership and build boundaries.**

  Keep shared client packages and public B contracts explicit; document native build prerequisites and clarify what local WSL can and cannot prove.

- [x] **Step 2: Make protocol examples operational.**

  Distinguish bearer API, browser HttpOnly session, native client HTTP, and WebSocket Origin requirements; do not imply that C changes A's authoritative terminal size.

- [x] **Step 3: Make security/deployment ownership explicit.**

  Document Admin Token exchange, TOTP reset only inside the Docker host/container, HTTPS/WSS at the reverse proxy, loopback-only insecure HTTP, and the non-persistence of terminal content.

### Task 4: Add documentation contract checks and verify

**Files:**
- Modify: `tests/docs/test_documentation_contract.py`

- [x] **Step 1: Assert the action guide names real workflow files and inputs.**
- [x] **Step 2: Assert the README links every current operator guide, including GitHub Actions.**
- [x] **Step 3: Run Markdown link/reference checks and the documentation contract tests.**
- [x] **Step 4: Run `git diff --check` and review all changed Markdown for stale variables or commands.**
- [x] **Step 5: Commit the documentation alignment as one focused change.**

Verification evidence before commit: `33 passed` from the documentation, packaging workflow, release
workflow, and Compose contract tests; all non-code-fence relative Markdown links resolve; `git diff --check`
is clean.
