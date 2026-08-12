# Computer A Bind-Mount Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Docker Computer A image initialize root-owned bind mounts without host-side `chown`, then run the long-lived TermFlow process as the non-root `termflow` user.

**Architecture:** The Node entrypoint owns exactly `/home/termflow` and `/work`. When PID 1 starts as root, it refuses symlinked or non-directory mount points, normalizes ownership without crossing filesystem boundaries, then re-execs itself through `setpriv`; all login, tmux, Bridge, and `termflow serve` work happens after the privilege drop. An explicit Docker `--user` override bypasses initialization by design.

**Tech Stack:** POSIX shell, Docker, `setpriv`, pytest contract tests, image smoke tests.

---

### Task 1: Freeze the image and documentation contract

**Files:**
- Modify: `tests/deploy/test_compose_contract.py`

- [x] Add assertions that the Node runtime no longer fixes `USER termflow`, that its entrypoint initializes only `/home/termflow` and `/work`, refuses symlinks, stays on each mounted filesystem, and uses `setpriv`.
- [x] Add assertions that the image verifier checks bind-mount initialization, non-root PID 1, and zero effective capabilities after the drop.
- [x] Add assertions that the README uses local bind mounts plus only `CHOWN`, `DAC_OVERRIDE`, `SETUID`, and `SETGID` startup capabilities.
- [x] Run `python -m pytest tests/deploy/test_compose_contract.py -q` and confirm the new assertions fail against the old image contract.

### Task 2: Implement the privileged initialization boundary

**Files:**
- Modify: `deploy/Dockerfile.node`
- Modify: `deploy/entrypoint.node.sh`

- [x] Remove the fixed runtime `USER` so Docker starts the entrypoint as root by default.
- [x] Add fixed path validation and `find -xdev` ownership normalization for `/home/termflow` and `/work`.
- [x] Re-exec the entrypoint as `termflow` with cleared supplementary groups before login or service startup.
- [x] Re-run `python -m pytest tests/deploy/test_compose_contract.py -q` and confirm the image contract passes.

### Task 3: Verify the real image boundary

**Files:**
- Modify: `scripts/verify-node-image.sh`

- [x] Add a symlink refusal smoke test.
- [x] Create root-owned host directories, mount them as identity/work storage, and verify the entrypoint makes them writable by `termflow` without host-side ownership changes.
- [x] Run with only `CHOWN`, `DAC_OVERRIDE`, `SETUID`, and `SETGID` added after `--cap-drop ALL`; assert PID 1 is non-root and `CapEff` is zero.
- [x] Run service-management commands through `docker exec --user termflow`; the Node metadata ownership check must never be bypassed by root.
- [x] Keep the no-TTY service, SIGTERM, restart identity, and single-instance checks under the same capability contract.

### Task 4: Update the installation path

**Files:**
- Modify: `README.md`

- [x] Replace named Node volumes with `mkdir -p termflow-node-identity termflow-node-work` and absolute local bind mounts.
- [x] Keep the example concise and do not require a host-side UID lookup or `chown`.
- [x] State that both directories are persistent A data and are managed by the container.

### Task 5: Verify and review

**Files:**
- Verify only

- [x] Run `python -m pytest tests/deploy/test_compose_contract.py -q`.
- [x] Build the image with `scripts/build-node-image.sh termflow-node:verify`.
- [x] Run `scripts/verify-node-image.sh termflow-node:verify`.
- [x] Run `./scripts/verify.sh`.
- [x] Run `git diff --check`, inspect the complete diff, and confirm unrelated files were not changed.
