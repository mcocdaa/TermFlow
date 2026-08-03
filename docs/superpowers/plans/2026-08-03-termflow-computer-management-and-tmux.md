# TermFlow Computer Management and tmux Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe Computer deletion and enrollment-completion feedback to the existing dark Computer table, and make the latest Node CLI diagnose/parse real-world tmux version output while preserving the 3.2+ requirement.

**Architecture:** Keep the current Vue table, enrollment dialog, FastAPI admin API, SQLite repositories, and `LiveInstanceRegistry`. Add one admin DELETE endpoint guarded by registry retirement plus Installation revocation; have the dialog poll the existing Computer list for a newly enrolled ID; harden `TmuxRunner._verify_version` without changing the tmux command interface.

**Tech Stack:** Vue 3 + TypeScript + Vitest, FastAPI + SQLAlchemy async repositories + pytest, Python 3.12, Typer/PyInstaller, Lucide Vue icons.

---

## File map

- Modify `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`: active Installation lookup, atomic revocation/deletion, and guard Instance registration against a revoked Installation.
- Modify `apps/control-plane/src/termflow_control_plane/api/computers.py`: admin DELETE route and registry retirement cleanup.
- Modify `apps/control-plane/src/termflow_control_plane/api/instances.py`: translate a revoked Installation registration race to the existing 401 error contract.
- Modify `apps/control-plane/tests/test_computers_api.py`, `apps/control-plane/tests/test_terms_api.py`, and `apps/control-plane/tests/test_repositories.py`: API, online-guard, credential invalidation, and repository race coverage.
- Modify `packages/client-core/src/api/computers.ts`: expose `remove(id, signal?)` as DELETE/204.
- Modify `packages/client-core/src/http/apiClient.test.ts`: URL encoding, method, and 204 contract.
- Modify `packages/client-ui/src/test/fakeRuntime.ts`: supply the new `computers.remove` fake.
- Modify `packages/client-ui/src/components/computers/ComputerTable.vue`: add operation header/cell, Lucide trash icon, disabled-online state, confirmation, and delete event.
- Modify `packages/client-ui/src/views/ComputersView.vue`: own delete requests, reload/message state, and enrollment `added` handling.
- Modify `packages/client-ui/src/components/computers/EnrollmentDialog.vue`: baseline/list polling, `added` emit, and timer cleanup.
- Modify `packages/client-ui/src/styles/app.css`: extend the existing grid to five columns and style the compact destructive action without changing the dark table theme.
- Modify `packages/client-ui/src/views/ComputersView.test.ts` and `packages/client-ui/src/components/computers/EnrollmentDialog.test.ts`: red-green UI behavior tests.
- Modify `apps/node/src/termflow_node/tmux/runner.py`: tolerant version extraction and safe diagnostic formatting.
- Modify `apps/node/tests/test_tmux_runner.py`: stdout/stderr/prefix/nonzero/low-version regression tests.

### Task 1: Add repository and API deletion protection

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/computers.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/instances.py`
- Test: `apps/control-plane/tests/test_computers_api.py`
- Test: `apps/control-plane/tests/test_repositories.py`

- [ ] **Step 1: Write failing repository/API tests.** Add a helper that provisions an Installation with one or more Instances. Assert that deleting an offline Computer returns 204, removes it from `GET /api/v1/computers`, makes its Installation bearer token return 401 for `/api/v1/instances/register`, and leaves unrelated Computers intact. Add an online websocket case asserting `409` with error code `computer_online` and no Installation/Instance mutation. Add unknown and repeated deletion cases asserting `404 computer_not_found`.

- [ ] **Step 2: Run the focused tests and verify the expected red failure.**

  Run:

  ```bash
  uv run --package termflow-control-plane pytest apps/control-plane/tests/test_computers_api.py -q
  ```

  Expected: the new tests fail because the DELETE route and repository method do not exist; existing list/rename tests remain green.

- [ ] **Step 3: Implement the repository operation.** Add `InstallationRevoked(RuntimeError)` next to `InstanceOwnershipError`. Add `InstallationRepository.delete(installation_id, now=None) -> bool` that, in one async session/transaction, selects an active Installation, updates `revoked_at`, deletes all `Instance` rows for that installation, commits, and returns false for missing/already-revoked IDs. In `InstanceRepository.register_or_rotate`, load the owning Installation and raise `InstallationRevoked` before creating/rotating an Instance when it is missing or revoked.

- [ ] **Step 4: Implement the API retirement sequence.** Add `@router.delete("/{installation_id}", status_code=204, dependencies=[Depends(require_admin)])`. Resolve the active Installation and its associated Instances; for each Instance call `registry.begin_retirement`. On `InstanceOnline`, cancel every retirement started in this request and raise `TermFlowError("computer_online", 409, "The Computer has an online Term.")`. Call the repository delete, cancel retirement markers after a successful delete, and raise `computer_not_found` if the repository reports false. On any repository exception, cancel the started markers before re-raising. Record `computer.delete` only after commit.

- [ ] **Step 5: Map the registration race safely.** Catch `InstallationRevoked` in `register_instance` and raise `TermFlowError("unauthorized", 401, "Authentication is required.")`; do not disclose whether a prior Installation existed.

- [ ] **Step 6: Run the focused tests and verify green.**

  Run:

  ```bash
  uv run --package termflow-control-plane pytest apps/control-plane/tests/test_computers_api.py apps/control-plane/tests/test_repositories.py -q
  ```

  Expected: all focused tests pass, including online refusal, offline deletion, repeated deletion, and token invalidation.

- [ ] **Step 7: Commit the backend slice.**

  ```bash
  git add -- apps/control-plane/src/termflow_control_plane/persistence/repositories.py apps/control-plane/src/termflow_control_plane/api/computers.py apps/control-plane/src/termflow_control_plane/api/instances.py apps/control-plane/tests/test_computers_api.py apps/control-plane/tests/test_repositories.py
  git commit -m "feat(control-plane): safely delete offline computers"
  ```

### Task 2: Expose Computer DELETE in the client API

**Files:**
- Modify: `packages/client-core/src/api/computers.ts`
- Modify: `packages/client-core/src/http/apiClient.test.ts`

- [ ] **Step 1: Write the failing client contract assertion.** In the existing fixed-path test call `api.computers.remove("computer /1")` and expect `request` to receive `['/api/v1/computers/computer%20%2F1', { method: 'DELETE' }]`.

- [ ] **Step 2: Run the focused Vitest test and verify it fails with `remove is not a function`.**

  ```bash
  npm test -- packages/client-core/src/http/apiClient.test.ts
  ```

- [ ] **Step 3: Implement `remove`.** Return `request<void>(... { method: 'DELETE' })` from `createComputersApi`, using `withSignal` exactly as `list`, `get`, and `rename` do.

- [ ] **Step 4: Run the client-core test and verify green.**

  ```bash
  npm test -- packages/client-core/src/http/apiClient.test.ts
  ```

- [ ] **Step 5: Commit the client API slice.**

  ```bash
  git add -- packages/client-core/src/api/computers.ts packages/client-core/src/http/apiClient.test.ts
  git commit -m "feat(client): add computer deletion request"
  ```

### Task 3: Add the screenshot-matched deletion control

**Files:**
- Modify: `packages/client-ui/src/components/computers/ComputerTable.vue`
- Modify: `packages/client-ui/src/views/ComputersView.vue`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts`
- Modify: `packages/client-ui/src/styles/app.css`
- Test: `packages/client-ui/src/views/ComputersView.test.ts`

- [ ] **Step 1: Write failing UI tests.** Add tests that mount one offline and one online Computer, assert a fifth `操作` header, one Lucide SVG trash button per row, `disabled` plus `aria-label` containing “在线” for the online row, and an offline click that calls `computers.remove` only after `window.confirm` returns true. Assert confirmation cancellation makes no request. Assert a successful delete removes the row and renders `已删除`; an `ApiError` renders its message.

- [ ] **Step 2: Run the focused UI test and verify the new assertions fail.**

  ```bash
  npm test -- packages/client-ui/src/views/ComputersView.test.ts
  ```

- [ ] **Step 3: Implement the table event.** Import `Trash2` from `@lucide/vue`, emit `remove: [computer: ComputerSummary]`, compute `onlineTermCount`, render the fifth column, and render a compact destructive icon button. Set `disabled` when the count is greater than zero; otherwise call `window.confirm` with the Computer display name and emit only after confirmation.

- [ ] **Step 4: Implement view ownership.** Pass `@remove="removeComputer"` to `ComputerTable`; add `loadComputers()` with an AbortController signal, `deletingId`, and `removeComputer` that calls `runtime.api.computers.remove`, filters the row, and sets `message` to `已删除`. Keep the existing API error mapping for failures.

- [ ] **Step 5: Update fake runtime and responsive styles.** Add a no-op `computers.remove` fake returning `undefined`. Change the table grid to five columns with the final action column sized `3rem`; keep the mobile single-column layout and add an action-cell label/align rule so the icon remains at the row edge without changing other columns.

- [ ] **Step 6: Run the focused UI tests and verify green.**

  ```bash
  npm test -- packages/client-ui/src/views/ComputersView.test.ts
  ```

- [ ] **Step 7: Commit the deletion UI slice.**

  ```bash
  git add -- packages/client-ui/src/components/computers/ComputerTable.vue packages/client-ui/src/views/ComputersView.vue packages/client-ui/src/test/fakeRuntime.ts packages/client-ui/src/styles/app.css packages/client-ui/src/views/ComputersView.test.ts
  git commit -m "feat(web): add safe computer delete control"
  ```

### Task 4: Close and refresh enrollment after successful login

**Files:**
- Modify: `packages/client-ui/src/components/computers/EnrollmentDialog.vue`
- Modify: `packages/client-ui/src/views/ComputersView.vue`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts`
- Test: `packages/client-ui/src/components/computers/EnrollmentDialog.test.ts`
- Test: `packages/client-ui/src/views/ComputersView.test.ts`

- [ ] **Step 1: Write failing dialog tests.** Add a controllable fake `clock.setInterval` and a `list` mock whose second response contains a new Computer with the entered display name. Assert that after creating the code, invoking the interval callback and flushing promises emits `added` once, clears the interval, and removes the code from the DOM. Add an unmount assertion that `clearInterval` runs and a same-name pre-existing Computer assertion that it does not emit.

- [ ] **Step 2: Run the dialog tests and verify the new assertions fail.**

  ```bash
  npm test -- packages/client-ui/src/components/computers/EnrollmentDialog.test.ts
  ```

- [ ] **Step 3: Implement baseline and polling.** Extend the dialog emits with `added: []`; before code creation, call `runtime.api.computers.list()` and store the baseline IDs (fall back to an empty set only when the list request fails and surface the existing error). After a successful token response, start a 1000ms interval that lists Computers, finds a new ID with matching `display_name`, calls `clearSecret()`, emits `added`, and stops. Add a separate polling guard so an in-flight list cannot start a second request; stop all timers in `close` and `onBeforeUnmount`.

- [ ] **Step 4: Wire the view response.** Render `<EnrollmentDialog @added="onComputerAdded" @closed="showEnrollment = false" />`. In `onComputerAdded`, close the dialog, call `loadComputers()`, and set `message` to `已添加`; retain any load error in the existing alert without reopening the secret.

- [ ] **Step 5: Add the view integration test and fake API.** Supply `computers.list`, `computers.createEnrollment`, and `computers.remove` in the fake runtime. Mount the view, open the dialog, complete the create response, make the next list response contain the new Computer, trigger the interval callback, and assert the dialog is gone, the new row is shown, and `已添加` is visible.

- [ ] **Step 6: Run both focused UI test files and verify green.**

  ```bash
  npm test -- packages/client-ui/src/components/computers/EnrollmentDialog.test.ts packages/client-ui/src/views/ComputersView.test.ts
  ```

- [ ] **Step 7: Commit the enrollment slice.**

  ```bash
  git add -- packages/client-ui/src/components/computers/EnrollmentDialog.vue packages/client-ui/src/views/ComputersView.vue packages/client-ui/src/test/fakeRuntime.ts packages/client-ui/src/components/computers/EnrollmentDialog.test.ts packages/client-ui/src/views/ComputersView.test.ts
  git commit -m "feat(web): close enrollment after computer login"
  ```

### Task 5: Harden tmux version detection

**Files:**
- Modify: `apps/node/src/termflow_node/tmux/runner.py`
- Test: `apps/node/tests/test_tmux_runner.py`

- [ ] **Step 1: Write failing parser tests.** Add fake-run cases for `stdout="version check: tmux 3.4 (Linux)\n"`, `stderr="tmux 3.4\n"` with empty stdout, `returncode=1` with diagnostic stderr, `stdout="tmux next-3.5\n"` with no numeric version, and `stdout="tmux 3.1\n"`. Assert the first two construct successfully, the nonzero/no-version case raises `TmuxUnavailable` containing return code and bounded output, the next-version case raises `TmuxUnavailable`, and 3.1 raises `UnsupportedTmuxVersion`.

- [ ] **Step 2: Run the focused Node tests and verify the new cases fail.**

  ```bash
  uv run --package termflow-node pytest apps/node/tests/test_tmux_runner.py -q
  ```

- [ ] **Step 3: Implement tolerant, bounded diagnostics.** Replace the anchored regex with a search regex for `tmux` followed by numeric major/minor components. Search the concatenated stdout/stderr only when output is non-empty; create a helper that replaces newlines with spaces and truncates each stream to 160 characters. If the return code is nonzero or no match exists, raise `TmuxUnavailable(f"Unable to determine tmux version (exit={...}; stdout=...; stderr=...)")`; preserve the existing missing-binary message and `UnsupportedTmuxVersion` wording.

- [ ] **Step 4: Run focused Node tests and the diagnostics test.**

  ```bash
  uv run --package termflow-node pytest apps/node/tests/test_tmux_runner.py apps/node/tests/test_diagnostics.py -q
  ```

- [ ] **Step 5: Commit the Node slice.**

  ```bash
  git add -- apps/node/src/termflow_node/tmux/runner.py apps/node/tests/test_tmux_runner.py
  git commit -m "fix(node): diagnose tmux version output"
  ```

### Task 6: Full verification and installed-package distinction

**Files:**
- Inspect only: `scripts/verify.sh`, `scripts/release/verify_node_bundle.sh`, `docs/troubleshooting.md`

- [ ] **Step 1: Run all directly affected Python and TypeScript tests.**

  ```bash
  uv run --all-packages pytest apps/control-plane/tests/test_computers_api.py apps/control-plane/tests/test_terms_api.py apps/control-plane/tests/test_repositories.py apps/node/tests/test_tmux_runner.py apps/node/tests/test_diagnostics.py -q
  npm test -- packages/client-core/src/http/apiClient.test.ts packages/client-ui/src/views/ComputersView.test.ts packages/client-ui/src/components/computers/EnrollmentDialog.test.ts
  ```

- [ ] **Step 2: Run repository verification after checking prerequisites.** Compare `node --version` with the pinned version in `scripts/verify.sh`; when it matches, run `bash scripts/verify.sh`. When it does not match, run the directly affected package tests plus the package typecheck/build commands available in `package.json`, and record the version mismatch instead of claiming the full script passed.

- [ ] **Step 3: Verify the source CLI and current installed binary separately.** Run `tmux -V`, `uv run --package termflow-node termflow doctor`, and `~/.local/bin/termflow --version`; do not claim the installed executable contains the fix until `scripts/release/verify_node_bundle.sh` or an equivalent rebuilt bundle test has passed.

- [ ] **Step 4: Review the final diff and status.** Confirm no `.superpowers/` visual session files are staged, no user data or live Term/tmux socket was touched, all expected requirements have a test or explicit evidence, and report source tests versus installed-package/live evidence separately.
