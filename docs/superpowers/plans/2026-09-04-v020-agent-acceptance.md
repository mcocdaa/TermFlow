# v0.2.0 Productized Agent Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce fresh, separated evidence that the complete Web C → B → OpenCode/DeepSeek → approval → Docker A `echo 1` path works and remains safe through lifecycle failures.

**Architecture:** Deterministic fake-provider tests prove behavior without external variability; disposable container tests prove topology; a stable named local stack proves persistence; and one bounded live DeepSeek scenario proves the external model path. Release documents consume evidence only after the corresponding gate passes.

**Tech Stack:** pytest cross-process harness, Playwright Chromium/device projects, Docker Compose/inspect, OpenCode 1.18.18, DeepSeek OpenAI-compatible API, TermFlow Docker A, repository verification scripts.

**Evidence update (2026-09-09):** Deterministic product/lifecycle and
disposable topology scenarios are green, including active-turn revocation,
SSE replay/deduplication, ambiguous-action parking, cleanup recovery and
dead-letter visibility. The full repository gate and a post-gate desktop
browser run are also green. A disposable B + Web C + OpenCode deployment
completed one real DeepSeek-to-Docker-A `echo 1` scenario and preserved its
state across B/OpenCode recreation. Its provider disclosure metadata was
synthetic/unverified, while the stable `termflow-v020-local` deployment env is
still missing 13 required fields; stable policy/release acceptance therefore
remains open and the no-training field stays fail-closed.

---

**Dependencies:** All preceding runtime, setup API, UI, cleanup/startup, and deployment plans must pass their focused gates and reviews.

**Execution constraint:** Never print or persist raw provider/admin/MCP/TOTP secrets in logs, docs, URLs, screenshots, or test artifacts. Preserve every pre-existing temporary project/volume. Delete only disposable resources created by the acceptance test itself; never use `down -v` on the stable project.

### Task 1: Deterministic Runtime and Lifecycle Integration

**Files:**

- Modify: `tests/e2e/test_agent_broker_process.py`
- Create: `tests/e2e/test_agent_product_setup.py`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Write cross-process RED scenarios**

Create tests with the exact names
`test_product_setup_activates_pipeline_without_b_restart`,
`test_runtime_health_drift_unmaps_then_recovers`,
`test_recovery_failure_keeps_core_up_and_inbox_unclaimed`,
`test_stale_epoch_wrong_host_and_revoked_token_fail_closed`, and
`test_cleanup_outage_returns_202_then_receipts_complete`. Record the B PID/start
counter, observed state transitions, gateway call count, inbox claim owner, and
cleanup job/receipt IDs at every boundary.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/e2e/test_agent_product_setup.py \
  tests/e2e/test_agent_broker_process.py
```

- [x] **Step 3: Extend only the reusable E2E fixtures**

Provide a deterministic fake OpenCode/provider with `/global/health`, `/mcp`,
session, prompt, SSE, abort, permission, and failure injection. Track B process
identity/start count so activation proves no B restart. Expose exact receipt and
inbox state through the product APIs where available; the acceptance helpers
may use read-only SQLite queries for durable run/inbox assertions (they never
mutate test state except for explicit recovery fault injection).

- [x] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/e2e/test_agent_product_setup.py \
  tests/e2e/test_agent_broker_process.py
git diff --check
```

Evidence: the combined deterministic product and cross-process suite passed
`9 passed in 107.43s` on 2026-09-08; the active-turn revoke case passed
separately (`1 passed in 10.72s`), for 10 exercised scenarios overall.

### Task 2: Real Browser Terminal-Sidecar Flow

**Files:**

- Create: `apps/clients/web/e2e/agent-terminal.spec.ts`
- Modify: `scripts/run-web-e2e.sh`

- [x] **Step 1: Write the browser RED test**

Cover login, Term open, Agent toggle, product setup, exact Pane selection,
disclosure checkbox, activating→ready, conversation creation, message send,
collapsed approval tray, timeline focus, fresh re-auth, approval, and terminal
result. Instrument terminal WebSocket creation and assert it remains exactly one
when the sidecar/query changes.

- [ ] **Step 2: Verify RED on desktop**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  ./scripts/run-web-e2e.sh agent-terminal.spec.ts --project=desktop
```

- [x] **Step 3: Add responsive/privacy assertions**

Desktop sidecar width is within 26–30rem. Mobile portrait/landscape uses an
overlay, terminal canvas/keybar are inert, approval is the sole alertdialog, and
focus returns to the toggle. Assert URL/storage/console contain none of the
message, prompt, terminal output, provider key, MCP token, admin token, or full
disclosure terms.

- [x] **Step 4: Verify GREEN across projects**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  ./scripts/run-web-e2e.sh agent-terminal.spec.ts --project=desktop
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  ./scripts/run-web-e2e.sh agent-terminal.spec.ts --project=mobile-portrait
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  ./scripts/run-web-e2e.sh agent-terminal.spec.ts --project=mobile-landscape
git diff --check
```

Evidence: the desktop run after the full gate passed `1 passed (5.5s)` on
2026-09-08; the portrait and landscape mobile projects had already passed in
the same acceptance run (`2 passed`). The separate RED transcript is not
retained, so that process-only checkbox remains deliberately open.

### Task 3: Disposable Live-Topology Security Verification

**Files:**

- Create: `tests/e2e/test_agent_provider_egress.py`
- Modify: `tests/e2e/test_agent_opencode_container.py`
- Modify: `scripts/security/verify-agent-containers.sh`

- [x] **Step 1: Write network RED scenarios**

Create tests named `test_allowed_provider_host_connects_through_proxy`,
`test_other_domain_raw_ip_and_other_port_are_denied`,
`test_opencode_direct_internet_path_is_absent`, and
`test_proxy_cannot_reach_control_plane_agent_network`. Use bounded curl probes
from the exact containers and assert exit status plus network membership; redact
destinations and never print proxy/provider environment.

- [ ] **Step 2: Verify RED in a unique disposable project**

```bash
TERMFLOW_E2E_OPENCODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/e2e/test_agent_provider_egress.py \
  tests/e2e/test_agent_opencode_container.py
```

- [x] **Step 3: Fix only proven topology/runtime defects**

Keep provider calls bounded and redact target URLs from output. The fixture owns
its random Compose project and records exact created resource IDs so teardown
can remove only those resources. Pre-existing project names and volumes are
asserted unchanged.

- [x] **Step 4: Verify GREEN and runtime inspection**

```bash
TERMFLOW_E2E_OPENCODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/e2e/test_agent_provider_egress.py \
  tests/e2e/test_agent_opencode_container.py
scripts/security/verify-agent-containers.sh --mode live termflow-v020-egress-audit
git diff --check
```

Evidence: the disposable uniquely named Compose project passed the provider
allow/deny and isolation suite (`4 passed in 52.53s`) and the pinned OpenCode
lifecycle smoke passed (`1 passed in 51.38s`); the live container inspector
ran inside the fixture and ownership-checked teardown removed its resources.
The separate RED transcript is not retained, so that process-only checkbox
remains deliberately open.

### Task 4: Start the Stable Local Stack and Prove Persistence

**Files:**

- Modify: `docs/runbooks/agent-broker-live-model.md`
- Create: `docs/runbooks/agent-broker-local.md`

- [ ] **Step 1: Verify provider disclosure source and local preflight**

Use the provider's official policy source to populate endpoint origin, region,
retention version/terms, and no-training truthfully. If `no_training=true`
cannot be supported, stop activation with the designed disclosure error rather
than overriding it. Then run:

```bash
scripts/deploy/agent-local-preflight.sh \
  --env-file /home/mcocdaa/AI_CODE/TermFlow/.env
```

Expected: exit 0, with variable names/status only. Mode must be 0600.

- [ ] **Step 2: Start without destructive recreation**

```bash
docker compose -p termflow-v020-local \
  --env-file /home/mcocdaa/AI_CODE/TermFlow/.env \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml up -d --build
```

Do not pass `--force-recreate` unless a specific changed service requires it;
never pass `-V` or delete volumes.

- [ ] **Step 3: Verify health, topology, and IDs**

Record safe container IDs, volume IDs, health states, and image digests. Run:

```bash
scripts/security/verify-agent-containers.sh --mode live termflow-v020-local
curl -fsS http://127.0.0.1:8765/healthz
```

Do not print inspect environments.

- [ ] **Step 4: Restart B and OpenCode, then compare persistence**

Restart the exact two stable containers. Verify volume IDs stay identical,
Profile/Binding/conversation data remains, normal B restart preserves epoch,
OpenCode reports MCP connected again, and runtime controller restores observed
ready before dispatch resumes.

### Task 5: Execute the Live DeepSeek → Approval → Docker A `echo 1` Flow

**Files:**

- Modify: `docs/runbooks/agent-broker-live-model.md` with a redacted evidence record
- Modify: `docs/releases/v0.2.0.md` only after success

- [x] **Step 1: Establish the product state through Web/API**

Start or reuse Docker A through the existing supported Docker A workflow. Log in
to Web C, open its Term, use setup UI to select the exact Pane, accept the
current disclosure, and wait for desired enabled + observed ready. Confirm the
provider state begins `configured_unverified`.

- [x] **Step 2: Ask the model for the exact bounded action**

Send: `请在当前允许的终端中执行 echo 1，并告诉我结果。` Wait for the proposed
write. No direct API may manufacture the approval or pane input.

- [ ] **Step 3: Perform fresh re-auth and approve once**

Let a deliberately stale session first receive 428, establish a fresh session
through the normal login/TOTP path, and retry the same decision once. Assert a
second decision/write is rejected.

- [ ] **Step 4: Prove the result at all boundaries**

Verify exactly one B approval consumption, exactly one command/request receipt,
exactly one Docker A execution receipt, terminal output containing the standalone
line `1`, Agent final response reporting `1`, and provider readiness changing to
`verified` for the current config revision. Record only IDs/digests/timestamps/
status codes; omit prompt content and secrets from durable evidence.

Evidence: the 2026-09-09 disposable `termflow-v020-live-0909` run established
the product state through the API with runtime `ready` and provider readiness
`configured_unverified`. The real model then proposed exactly one reviewed
`send_text`; one approval was consumed, a duplicate decision returned HTTP 409,
Docker A contained one `echo 1` command and one standalone `1`, and the sole
final assistant body was exactly `1`. Steps 3 and 4 remain open because this run
did not exercise the deliberately stale 428 path and provider readiness did not
become `verified`; synthetic disclosure metadata is not policy evidence.

### Task 6: Exercise Adversarial Restart, Revoke, Disconnect, and Cleanup

**Files:**

- Modify: `tests/e2e/test_agent_product_setup.py`
- Modify: `docs/runbooks/agent-broker-live-model.md`

- [x] **Step 1: Verify capability fences**

Check old token, old epoch, wrong host, revoked Binding, and revoke-during-turn.
Each must fail closed and leave no extra A receipt.

- [x] **Step 2: Verify disconnect/reconcile**

Interrupt OpenCode SSE during a non-side-effecting turn and verify bounded
reconnect/replay. Interrupt during an action-producing ambiguous outcome and
verify `delivery_unknown` without automatic resubmit.

- [x] **Step 3: Verify cleanup receipts**

Make the backend/deployment helper unavailable, initiate deletion, and assert
202 with the same job on repeat. Restore the dependency, confirm exact receipts,
and reach completed. Separately force one dead-letter and prove it stays visible.
Compare pre-existing unrelated volume/container IDs before and after.

Evidence: `tests/e2e/test_agent_product_setup.py` covers stale epoch,
wrong-host, revoked token/binding, active-turn revocation, non-side-effect SSE
replay/deduplication, ambiguous action `delivery_unknown` without a second
prompt, 202 cleanup retry with the same job, and persistent visible
`dead_letter`. The focused product/cross-process suites were green on
2026-09-08; disposable container checks left no
`termflow-egress-audit-*` or `termflow-opencode-e2e-*` resources and preserved
the existing `termflow-v020-local-0903-*` resources.

### Task 7: Full Verification and Release Bookkeeping

**Files:**

- Modify only after matching evidence: `docs/superpowers/plans/2026-08-10-termflow-0.2.0-agent-broker.md`
- Modify only after matching evidence: `docs/superpowers/plans/2026-08-22-v020-m4-m8-completion.md`
- Modify only after matching evidence: `docs/releases/v0.2.0.md`

- [x] **Step 1: Run the complete repository gate fresh**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH ./scripts/verify.sh
```

- [ ] **Step 2: Re-run the product browser and live smoke after full gate**

Run the desktop `agent-terminal.spec.ts` once more against the stable local
stack and perform a bounded API readiness/receipt query. Do not repeat a paid
model action if the existing live record is for the same immutable config
revision and remained valid through the full gate.

- [x] **Step 3: Update only evidenced checkboxes**

M4/M8 items receive exact command/date/result pointers. Keep any unexecuted
device/platform gate unchecked. Do not tag, merge, push, commit, or remove old
resources without a separate user instruction.

- [x] **Step 4: Final diff and secret audit**

```bash
git diff --check
git status --short
```

Search tracked/untracked text and generated browser artifacts for known secret
variable values using a non-printing match command. Report only match counts and
paths after redaction; never echo the matching line.

Evidence: `scripts/verify.sh` exited 0 on 2026-09-08 after refreshing the
generated client contract; it reported `537 passed, 7 skipped`, clean Ruff and
mypy, successful Rust/Tauri checks, Compose rendering, and Control Plane image
verification. A post-gate desktop Agent terminal run passed. The live-smoke
portion of Step 2 remains open because the stable deployment preflight cannot
pass without operator-supplied provider-policy evidence.
