# v0.2.0 Cleanup Receipts and Startup Fencing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop reporting queued deletion as complete and prevent Agent work from starting after critical recovery failure.

**Architecture:** Migration `0012` adds an idempotent cleanup job manifest with per-artifact receipts. A focused cleanup coordinator drives B-owned receipts and accepts narrow deployment-helper confirmations, while an explicit startup coordinator gates watch/pipeline/dispatcher activation behind successful fencing.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, pytest, existing plugin lifecycle and capability APIs.

---

**Dependency:** Migration `0011` and the runtime controller must be complete and reviewed. `0012` is the only new Alembic head in this plan.

**Execution constraint:** One cleanup/startup implementer owns backend hotspots. Preserve dirty work; do not commit/push/reset/clean. Do not mount Docker into B.

### Task 1: Add Migration `0012` and Cleanup Receipt Models

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0012_agent_cleanup_receipts.py`
- Create: `apps/control-plane/tests/test_agent_cleanup_migration.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/tests/test_agent_plugin_cleanup.py`

- [ ] **Step 1: Write migration RED tests**

Create tests with the exact names `test_0012_upgrades_populated_0011_database`,
`test_0012_empty_database_reaches_head`,
`test_0012_downgrades_back_to_0011`,
`test_cleanup_receipt_unique_manifest_entry`, and
`test_legacy_job_becomes_visible_dead_letter`. Inspect SQLite schema/indexes,
exercise duplicate insertion, and assert the migrated legacy job and receipt
both carry the stable dead-letter state/reason.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_migration.py \
  apps/control-plane/tests/test_agent_plugin_cleanup.py::test_migration_manifest_declares_the_full_alembic_chain
```

- [ ] **Step 3: Implement the schema**

```python
class AgentCleanupReceipt(Base):
    __tablename__ = "agent_cleanup_receipts"
    id: Mapped[UUID]
    cleanup_job_id: Mapped[UUID]
    artifact_kind: Mapped[str]
    artifact_ref: Mapped[str]
    state: Mapped[str]
    attempt_count: Mapped[int]
    last_error: Mapped[str | None]
    next_attempt_at: Mapped[datetime | None]
    evidence_digest: Mapped[str | None]
    policy_reason: Mapped[str | None]
    policy_version: Mapped[str | None]
    confirmed_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

Add job `manifest_version` and `completed_at`, receipt unique
`(cleanup_job_id, artifact_kind, artifact_ref)`, due index, and one job per
`(target_kind,target_ref)`. Existing jobs receive a `legacy_manifest`
dead-letter receipt and stable reason `legacy_cleanup_manifest_unavailable`;
they must not auto-complete with zero receipts.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_migration.py \
  apps/control-plane/tests/test_agent_plugin_cleanup.py
git diff --check
```

### Task 2: Implement Cleanup Repository and Coordinator

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/cleanup.py`
- Create: `apps/control-plane/tests/test_agent_cleanup_receipts.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/tests/test_agent_plugin_cleanup.py`
- Modify: `apps/control-plane/tests/test_agent_purge_retention.py`

- [ ] **Step 1: Write receipt-state RED tests**

Create tests with those seven exact names. Run concurrent manifest creation with
the same target; assert one job and an identical receipt set. Progress one
receipt at a time and inspect aggregate state after each transition. Exercise
missing handler, incomplete policy metadata, same-evidence replay,
different-evidence conflict, and purge time beyond retention for dead-letter.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_receipts.py
```

- [ ] **Step 3: Implement the coordinator contract**

```python
@dataclass(frozen=True, slots=True)
class CleanupReceiptSeed:
    artifact_kind: str
    artifact_ref: str
    state: Literal["pending", "not_applicable"] = "pending"
    policy_reason: str | None = None
    policy_version: str | None = None


class AgentCleanupCoordinator:
    def __init__(self, repositories, handlers, clock) -> None:
        self._repositories = repositories
        self._handlers = handlers
        self._clock = clock
```

Add async method `create_or_get_manifest` with keyword arguments `target_kind`,
`target_ref`, `receipts`, `term_id`, `installation_id`, and `manifest_version`,
returning `AgentCleanupJob`; add `process_due_receipts(job_id, now) ->
CleanupJobResult` and `confirm_receipt(receipt_id, artifact_ref,
evidence_digest) -> AgentCleanupReceipt`.

Manifest and receipts are created atomically. Aggregate completion requires all
receipts confirmed or policy-valid not-applicable. Artifact references reject
credentials, query strings, headers, and provider payloads.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_receipts.py \
  apps/control-plane/tests/test_agent_plugin_cleanup.py \
  apps/control-plane/tests/test_agent_purge_retention.py
git diff --check
```

### Task 3: Return `202 deletion_pending` and Expose Safe Job Status

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/api/agent_cleanup.py`
- Create: `apps/control-plane/tests/test_agent_cleanup_api.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_conversations.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_admin.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/terms.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/computers.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/tests/test_agent_delete_contract.py`
- Modify: `apps/control-plane/tests/test_computers_api.py`

- [ ] **Step 1: Write DELETE semantic RED tests**

Create tests with those five exact names. Parameterize conversation, Binding,
Profile, Term, and Computer deletion; compare cleanup job IDs across repeats;
assert the 202 body contains only job ID/state/status URL and the status response
contains neither artifact refs nor injected internal errors.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_delete_contract.py \
  apps/control-plane/tests/test_computers_api.py \
  apps/control-plane/tests/test_agent_cleanup_api.py
```

- [ ] **Step 3: Implement response contracts and routing**

```python
class CleanupPendingResponse(BaseModel):
    cleanup_job_id: UUID
    state: Literal["deletion_pending"]
    status_url: str


class CleanupJobResponse(BaseModel):
    cleanup_job_id: UUID
    state: Literal["pending", "completed", "dead_letter"]
    receipts: list[CleanupReceiptResponse]
```

Collect all opaque artifact references before parent cascade and create the
manifest first. Return 204 only after aggregate completion; otherwise return 202
with the same active job. `GET /api/v1/agent/admin/cleanup-jobs/{id}` exposes
kind/state/safe reason only.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_delete_contract.py \
  apps/control-plane/tests/test_computers_api.py \
  apps/control-plane/tests/test_agent_cleanup_api.py
git diff --check
```

### Task 4: Generate Cleanup Contracts and Preserve Pending UI State

**Files:**

- Modify: `scripts/generate-client-contracts/generate.py`
- Modify: `packages/client-contracts/src/generated.ts` (generator output only)
- Modify: `packages/client-core/src/api/agents.ts`
- Modify: `packages/client-core/src/api/terms.ts`
- Modify: `packages/client-core/src/api/computers.ts`
- Create: `packages/client-core/src/api/cleanup.test.ts`
- Modify: `packages/client-ui/src/components/dashboard/DeleteTermDialog.vue`
- Create: `packages/client-ui/src/components/dashboard/DeleteTermDialog.test.ts`
- Modify: `packages/client-ui/src/components/computers/DeleteComputerDialog.vue`
- Create: `packages/client-ui/src/components/computers/DeleteComputerDialog.test.ts`

- [ ] **Step 1: Write 202-response RED tests**

Create client tests that feed 202 and 204 responses to Term/Computer/Agent
deletion. Assert 202 returns the typed cleanup job ID/status URL, 204 returns a
completed discriminant, and neither path discards the body before parsing. Add
component tests that keep a visible pending state/job reference instead of
announcing completed deletion.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- src/api/cleanup.test.ts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-ui -- \
  src/components/dashboard/DeleteTermDialog.test.ts \
  src/components/computers/DeleteComputerDialog.test.ts
```

- [ ] **Step 3: Generate and consume a discriminated result**

```typescript
export type DeleteResult =
  | { state: 'completed' }
  | { state: 'deletion_pending'; cleanupJobId: string; statusUrl: string }
```

Extend the generator for cleanup responses, regenerate contracts, and make all
delete APIs inspect response status/body. Pending UI states show the job ID in a
safe bounded message and never use success wording.

- [ ] **Step 4: Verify GREEN and generator drift**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run contracts:generate
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run contracts:check
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run test:run --workspace @termflow/client-core -- src/api/cleanup.test.ts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run test:run --workspace @termflow/client-ui
git diff --check
```

### Task 5: Add Narrow Deployment-Helper Confirmation

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_cleanup.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/tests/test_agent_cleanup_api.py`
- Modify: `.env.example`

- [ ] **Step 1: Write helper-auth RED tests**

Create tests with those four exact names. Use distinct admin/helper bearer
fixtures, a mismatched exact artifact reference, 63/64/non-hex digest cases, and
same versus different idempotency keys/evidence; assert the receipt row is
unchanged for every rejected call.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_api.py
```

- [ ] **Step 3: Implement the narrow request/dependency**

```python
class CleanupReceiptConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_ref: str
    result: Literal["confirmed", "dead_letter"]
    evidence_digest: str | None
    reason_code: str | None
    idempotency_key: UUID
```

`require_cleanup_helper` accepts only the independent hashed/constant-time
`TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN`. Confirmed requires a 64-hex digest;
dead-letter requires a bounded public reason. The route is
`POST /api/v1/agent/admin/cleanup-jobs/{job_id}/receipts/{receipt_id}/confirm`.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_api.py \
  apps/control-plane/tests/test_config.py
git diff --check
```

### Task 6: Gate Agent Activation With Startup Fencing

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/startup.py`
- Create: `apps/control-plane/tests/test_agent_startup.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/agent_contracts.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_capabilities.py`
- Modify: `apps/control-plane/tests/test_app_wiring.py`

- [ ] **Step 1: Write startup RED tests**

Create tests with those six exact names. Inject separate critical fencing and
per-Binding runtime failures; count calls to watch/runtime/pipeline/dispatcher/
cleanup hooks; query `/healthz`, capabilities, observed Binding state, and inbox
claim fields after startup and one successful retry.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_startup.py \
  apps/control-plane/tests/test_app_wiring.py
```

- [ ] **Step 3: Implement the explicit coordinator**

```python
class AgentStartupState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class AgentStartupResult:
    state: AgentStartupState
    reason_code: str | None
    critical_failures: tuple[str, ...]


class AgentStartupCoordinator:
    def __init__(
        self, fencing, controller, topology, backend, watches, dispatcher, cleanup
    ) -> None:
        self._fencing = fencing
        self._controller = controller
        self._topology = topology
        self._backend = backend
        self._watches = watches
        self._dispatcher = dispatcher
        self._cleanup = cleanup
```

Add async `recover() -> AgentStartupResult` and
`activate() -> AgentStartupResult` methods. `activate()` is callable only after
successful recovery and is idempotent.

Critical DB/fencing failure prevents watch rebuild, runtime publish, pipeline
start, dispatcher start, and inbox claim. Cleanup retry can run. Individual A or
OpenCode outage becomes Binding `not_ready`, not global degraded. `/healthz`
stays 200 while Agent capabilities report `degraded/recovery_failed`.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_startup.py \
  apps/control-plane/tests/test_app_wiring.py \
  apps/control-plane/tests/test_agent_runtime_controller.py \
  apps/control-plane/tests/test_agent_runtime_registry.py
git diff --check
```

### Task 7: Cleanup and Startup Slice Verification

**Files:** Verify only.

- [ ] **Step 1: Run focused regression**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_cleanup_migration.py \
  apps/control-plane/tests/test_agent_cleanup_receipts.py \
  apps/control-plane/tests/test_agent_cleanup_api.py \
  apps/control-plane/tests/test_agent_delete_contract.py \
  apps/control-plane/tests/test_agent_purge_retention.py \
  apps/control-plane/tests/test_agent_startup.py \
  apps/control-plane/tests/test_app_wiring.py
.venv/bin/ruff check apps/control-plane/src apps/control-plane/tests
.venv/bin/mypy apps/control-plane/src
git diff --check
```

- [ ] **Step 2: Record exact evidence**

Record exit codes and counts. The slice needs spec review and quality review;
pending/dead-letter states must remain visible in tests before handoff.
