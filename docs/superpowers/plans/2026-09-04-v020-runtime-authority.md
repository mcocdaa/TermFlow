# v0.2.0 Runtime Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Profiles executable, separate desired Binding state from observed readiness, persist provider consent, enforce fresh admin authentication, and activate OpenCode pipelines without restarting B.

**Architecture:** A server-owned provider catalog validates canonical Profile JSON and disclosure fingerprints. Migration `0011` adds desired/observed/runtime/auth state, while a single `AgentRuntimeController` reconciles desired revisions into atomically published registry pipelines. Sensitive mutations consume an `AdminAuthContext` whose original strong-auth time cannot be refreshed into freshness.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, httpx, pytest/pytest-asyncio, SQLite.

---

**Source spec:** `docs/superpowers/specs/2026-09-04-v020-productized-agent-completion-design.md`

**Execution constraint:** One runtime implementer owns all backend files in this plan. Preserve existing dirty changes. Do not commit, push, reset, or clean; use `git diff --check` as each task checkpoint.

### Task 1: Canonical Profile Configuration and Provider Catalog

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/provider_catalog.py`
- Create: `apps/control-plane/tests/test_agent_profile_config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/agent_contracts.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_admin.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/runtime_registry.py`
- Modify: `apps/control-plane/tests/test_agent_epoch_invalidation.py`
- Modify: `apps/control-plane/tests/test_agent_runtime_registry.py`
- Modify: direct Profile HTTP fixtures under `apps/control-plane/tests/`,
  `tests/e2e/test_agent_broker_process.py`, and
  `apps/clients/web/e2e/agent-chat.spec.ts`

- [x] **Step 1: Write focused failing tests**

```python
def test_profile_config_accepts_only_canonical_provider_and_model() -> None:
    parsed, encoded = canonicalize_profile_config(
        {"provider_id": "deepseek", "model_id": "deepseek-v4-flash"}
    )
    assert parsed.provider_id == "deepseek"
    assert encoded == '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}'


def test_disclosure_fingerprint_changes_for_every_server_owned_field() -> None:
    first = catalog.disclosure_fingerprint(config)
    changed = replace(catalog_entry, retention_version="v2")
    assert ProviderCatalog([changed]).disclosure_fingerprint(config) != first
```

- [x] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_profile_config.py
```

Expected: collection/import failure for the missing `provider_catalog` module.

- [x] **Step 3: Implement the validated types**

```python
class AgentProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    provider_id: str
    model_ids: frozenset[str]
    endpoint_origin: str
    region: str
    retention_terms: str
    retention_version: str
    no_training: bool
    credential_source: str
    policy_version: str


def canonicalize_profile_config(value: object) -> tuple[AgentProfileConfig, str]:
    parsed = AgentProfileConfig.model_validate(value)
    encoded = json.dumps(
        parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return parsed, encoded
```

`ProviderCatalog.resolve()` rejects unknown provider/model with stable codes.
`disclosure_fingerprint()` hashes canonical JSON containing every disclosure
field but no credential. Admin Profile create/update stores only the canonical
encoded string.

- [x] **Step 4: Verify GREEN and regression scope**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_profile_config.py \
  apps/control-plane/tests/test_agent_profile_conflicts.py
git diff --check
```

Expected: selected tests pass and diff check is silent.

### Task 2: Make OpenCode Prompts Consume the Profile

**Files:**

- Create: `apps/control-plane/tests/test_opencode_adapter.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/opencode.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/runtime_registry.py`
- Modify: `apps/control-plane/tests/test_opencode_sse.py`

- [x] **Step 1: Write the failing request-body test**

```python
async def test_submit_sends_selected_provider_and_model() -> None:
    adapter = OpenCodeAdapter(
        base_url="http://runtime",
        directory="/workspace",
        backend_version="1.18.18",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        transport=transport,
    )
    await adapter.submit(request)
    assert recorded_json["model"] == {
        "providerID": "deepseek",
        "modelID": "deepseek-v4-flash",
    }
```

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_opencode_adapter.py::test_submit_sends_selected_provider_and_model
```

Expected: constructor/body assertion fails because provider/model are not wired.

- [x] **Step 3: Add explicit adapter fields and payload**

Add required `provider_id` and `model_id` constructor arguments and include:

```python
payload = {
    "parts": parts,
    "model": {"providerID": self._provider_id, "modelID": self._model_id},
}
```

Registry adapter construction obtains the parsed Profile config; no global
default silently substitutes for a malformed Profile.

- [x] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_opencode_adapter.py \
  apps/control-plane/tests/test_opencode_sse.py \
  apps/control-plane/tests/test_agent_runtime_registry.py
git diff --check
```

### Task 3: Add Migration `0011` and SQLAlchemy Models

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0011_agent_runtime_state.py`
- Create: `apps/control-plane/tests/test_agent_runtime_state_migration.py`
- Create: `apps/control-plane/tests/test_agent_runtime_binding_repository.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/migrations/env.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/database.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/tests/test_agent_plugin_cleanup.py`

- [x] **Step 1: Write migration RED tests**

Create three tests named
`test_0010_to_0011_maps_desired_without_inventing_observed_ready`,
`test_0011_partial_unique_index_allows_only_one_non_revoked_binding`, and
`test_0011_round_trip_preserves_runtime_refs_and_epochs`. Assert
`pending -> disabled`, `ready -> enabled`, a duplicate non-revoked row raises an
integrity error, revoked history remains insertable, and zero observed row is
marked ready solely because the old desired string was ready.

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_state_migration.py \
  apps/control-plane/tests/test_agent_plugin_cleanup.py::test_migration_manifest_declares_the_full_alembic_chain
```

Expected: missing revision/table/columns.

- [x] **Step 3: Implement schema exactly**

Add `AgentBinding.config_revision`, expanded `AgentRuntimeBinding` with
`UNIQUE(binding_id)`, `AgentProviderDisclosureAcceptance`, and
`AuthToken.authenticated_at`. Add indexes for due/observed lookups and a partial
unique index for non-revoked `(profile_id, term_id)` rows. Upgrade data before
replacing the old uniqueness constraint; downgrade reverses the mapping without
dropping stored runtime identity before copy-back.

```python
class AgentProviderDisclosureAcceptance(Base):
    __tablename__ = "agent_provider_disclosure_acceptances"
    id: Mapped[UUID]
    binding_id: Mapped[UUID]
    disclosure_fingerprint: Mapped[str]
    provider_id: Mapped[str]
    model_id: Mapped[str]
    endpoint_origin: Mapped[str]
    region: Mapped[str]
    retention_terms: Mapped[str]
    retention_version: Mapped[str]
    no_training: Mapped[bool]
    policy_version: Mapped[str]
    accepted_at: Mapped[datetime]
    accepted_auth_epoch: Mapped[int]
    actor_kind: Mapped[str]
    actor_ref: Mapped[str]
    revoked_at: Mapped[datetime | None]
```

- [x] **Step 4: Verify upgrade, downgrade, and manifest**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_state_migration.py \
  apps/control-plane/tests/test_agent_plugin_cleanup.py
git diff --check
```

### Task 4: Persist Desired/Observed State Through Repositories

**Files:**

- Create: `apps/control-plane/tests/test_agent_runtime_state_repository.py`
- Create: `apps/control-plane/tests/test_agent_provider_disclosure.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`

- [x] **Step 1: Write repository RED tests**

Create these four required tests:

- `test_update_desired_runtime_model_only_advances_revision_without_rotating_epoch`
- `test_update_desired_runtime_authority_change_advances_revision_and_rotates_epoch_once`
- `test_compare_and_set_ready_rejects_stale_revision_without_mutating_observed_state`
- `test_current_disclosure_lookup_rejects_revoked_or_nonmatching_acceptance`

Also add focused coverage named
`test_mark_reconciling_requires_enabled_expected_revision_and_upserts_one_row`
and
`test_mark_unavailable_cannot_synthesize_ready_and_preserves_applied_identity`.
Compare before/after revision and epoch values for model-only and
authority-changing updates. Assert an old desired revision cannot replay or
rotate twice. Exercise both the successful and stale final CAS paths; the stale
CAS must return false and leave every observed field, including `updated_at`,
unchanged. Assert a revoked or non-matching disclosure lookup returns no current
acceptance while retaining history.

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_state_repository.py \
  apps/control-plane/tests/test_agent_provider_disclosure.py
```

- [x] **Step 3: Implement focused repository methods**

Implement exact async methods `update_desired_runtime(binding_id, runtime_ref,
capability_ref, expected_revision, rotate_epoch)`,
`mark_reconciling(binding_id, expected_revision, fingerprint)`,
`compare_and_set_ready(binding_id, expected_revision, fingerprint,
observed_epoch)`, and `mark_unavailable(binding_id, readiness, reason_code)`.

`update_desired_runtime` is one atomic revision CAS. It advances
`config_revision` for every accepted change, rotates `runtime_epoch` exactly
once when requested, and rejects a runtime/capability identity change without
epoch rotation. `mark_reconciling` requires the desired Binding to remain
`enabled` at the expected revision and preserves the last applied identity.
The final ready CAS must atomically require desired `enabled`, matching revision
and epoch, non-null desired runtime/capability, the same observed reconciling
fingerprint, and a current non-revoked disclosure acceptance. A failed CAS
changes nothing. `mark_unavailable` preserves last-applied identity and rejects
`ready` as an input.

Add focused disclosure history methods for immutable acceptance insertion,
current exact-fingerprint lookup, and revoking all current acceptances for a
Binding. No method accepts or persists provider credentials. Register the
repository in `RepositoryBundle`.

Keep transactions inside repository methods atomic and use database expressions
for revision/epoch increments. No caller may synthesize observed `ready` by
updating an ORM object directly; legacy `upsert`/`set_readiness` entry points must
reject `ready` or become private. This task does not yet rewrite API/controller
callers; those integrations remain in their later named tasks.

- [x] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_state_repository.py \
  apps/control-plane/tests/test_agent_provider_disclosure.py \
  apps/control-plane/tests/test_repository_edges.py
git diff --check
```

### Task 5: Add Fresh `AdminAuthContext`

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/auth/context.py`
- Create: `apps/control-plane/tests/test_auth_freshness.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/service.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/oauth.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/oauth.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`

- [x] **Step 1: Write freshness RED tests**

Create tests with the exact names
`test_browser_session_retains_original_authenticated_at`,
`test_refresh_rotation_preserves_original_authenticated_at`,
`test_raw_root_bearer_is_admin_but_not_fresh_admin`, and
`test_expired_freshness_returns_428_stable_code`. Also add
`test_prompt_login_cannot_reuse_existing_browser_session`. Freeze the clock and assert
refresh preserves the first timestamp byte-for-byte, raw root passes normal
admin dependency but receives 428 from fresh dependency, and a session at 301
seconds is stale while one at exactly 300 seconds is fresh. A native
`prompt=login` authorization must require a new root/TOTP authentication and
must not approve from an existing browser cookie alone.

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_auth_freshness.py \
  apps/control-plane/tests/test_oauth_device_flow.py
```

- [x] **Step 3: Implement context and dependencies**

```python
@dataclass(frozen=True, slots=True)
class AdminAuthContext:
    credential_kind: str
    actor_ref: str
    auth_epoch: int
    authenticated_at: datetime | None

    def is_fresh(self, *, now: datetime, maximum_age: timedelta) -> bool:
        return self.authenticated_at is not None and (
            timedelta(0) <= now - self.authenticated_at <= maximum_age
        )
```

Add `agent_sensitive_action_max_age_seconds=300`. Browser session creation sets
the time after successful root/TOTP authentication. Auth token rotation copies
the parent time. Native tokens derive it from OAuth `approved_at`. Existing
admin-only routes continue to accept every valid admin context.

- [x] **Step 4: Verify GREEN and existing auth behavior**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_auth_freshness.py \
  apps/control-plane/tests/test_totp_api.py \
  apps/control-plane/tests/test_oauth_device_flow.py \
  tests/e2e/test_unified_auth.py
git diff --check
```

### Task 6: Require Health Plus Connected MCP

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/http_runtime_client.py`
- Modify: `apps/control-plane/tests/test_http_runtime_client.py`
- Modify: `apps/control-plane/tests/test_runtime_supervisor.py`

- [x] **Step 1: Add the missing RED cases**

Create tests named `test_readiness_requires_termflow_mcp_connected`,
`test_readiness_rejects_missing_failed_or_malformed_mcp_state`, and
`test_probe_does_not_echo_endpoint_or_credentials_in_public_reason`. Feed an
httpx mock transport healthy health responses paired with connected, missing,
failed, and malformed MCP payloads; assert only connected succeeds and all
public reasons are from the fixed code set.

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_http_runtime_client.py::test_readiness_requires_termflow_mcp_connected
```

- [x] **Step 3: Implement a bounded readiness result**

```python
@dataclass(frozen=True, slots=True)
class RuntimeReadinessProbe:
    healthy: bool
    mcp_connected: bool
    reason_code: str | None
```

Probe authenticated `/global/health`, then `/mcp`, and accept only an object
whose TermFlow entry reports `status == "connected"`. Keep raw response bodies,
URLs, auth headers, and exceptions out of the persisted/public reason.

- [x] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_http_runtime_client.py \
  apps/control-plane/tests/test_runtime_supervisor.py
git diff --check
```

### Task 7: Make Registry Publication Atomic

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/runtime_registry.py`
- Modify: `apps/control-plane/tests/test_agent_runtime_registry.py`

- [x] **Step 1: Write candidate lifecycle RED tests**

Create tests with the exact names
`test_candidate_is_not_visible_until_pipeline_started`,
`test_start_failure_closes_candidate_and_keeps_old_mapping`, and
`test_same_binding_replace_stops_old_runtime_after_publish`. Assert
`pipeline_for()` remains the old/None mapping until publish, candidate adapter
close is called once on start failure, and successful replacement publishes the
new mapping before stopping the old one.

- [x] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_registry.py::test_candidate_is_not_visible_until_pipeline_started
```

- [x] **Step 3: Split construction from publication**

```python
@dataclass(slots=True)
class RuntimeCandidate:
    pipeline: AgentPipelineService
    adapter: AgentBackend
    scope: BackendEventScope
    capabilities: AgentBackendCapabilities
    runtime_ref: RuntimeRef
    runtime_epoch: int
    capability_ref: str
    base_url: str
    directory: str
```

Implement registry methods `build_candidate(binding)`,
`start_candidate(candidate)`, `publish_started(binding_id, candidate)`, and
`unpublish(binding_id)` with the return types described by `RuntimeCandidate`.

Endpoint isolation checks consider the live mapping and candidates under the
controller's endpoint lock. Candidate failure always stops/closes candidate
resources and never overwrites the live mapping.

- [x] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_registry.py \
  apps/control-plane/tests/test_agent_pipeline.py
git diff --check
```

### Task 8: Implement `AgentRuntimeController`

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/runtime_controller.py`
- Create: `apps/control-plane/tests/test_agent_runtime_controller.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/runtime_registry.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/plugin.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`

- [ ] **Step 1: Write controller RED tests**

Create tests named `test_activate_without_b_restart_publishes_ready_pipeline`,
`test_stale_revision_never_publishes_candidate`,
`test_health_drift_unmaps_and_persists_not_ready`, and
`test_reconcile_is_idempotent_and_serialized_per_binding`. Assert the B process
identity does not change, stale candidates are closed, unpublish precedes the
not-ready write, and two concurrent reconciles produce one candidate start.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_controller.py
```

- [ ] **Step 3: Implement reconcile/fence lifecycle**

```python
@dataclass(frozen=True, slots=True)
class RuntimeReconcileResult:
    binding_id: UUID
    readiness: str
    reason_code: str | None
    applied_revision: int | None
```

Implement `AgentRuntimeController.reconcile(binding_id)`, `reconcile_all()`,
`fence(binding_id, rotate_epoch)`, and `run_health_cycle()` with the result type
above.

Use one lock per Binding and one endpoint-isolation lock. Perform the final
desired revision/disclosure fingerprint CAS after candidate start and before
publish. A failed CAS closes the candidate. A health-cycle mismatch unpublishes
before persisting `not_ready`.

- [ ] **Step 4: Verify GREEN and wiring**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_runtime_controller.py \
  apps/control-plane/tests/test_agent_runtime_registry.py \
  apps/control-plane/tests/test_app_wiring.py
git diff --check
```

### Task 9: Persist Provider Verification Separately From Runtime Readiness

**Files:**

- Create: `apps/control-plane/tests/test_agent_provider_verification.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/pipeline.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`

- [ ] **Step 1: Write provider-state RED tests**

Create tests named `test_runtime_activation_sets_configured_unverified`,
`test_first_assistant_output_marks_current_revision_verified`,
`test_stale_run_cannot_verify_new_revision`, and
`test_provider_auth_failure_sets_safe_failed_reason`. Assert runtime readiness
remains independently ready, the stored verified revision matches the run's
captured config revision, and raw provider errors never enter the row/API.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_provider_verification.py
```

- [ ] **Step 3: Implement event-driven verification**

Controller activation writes `configured_unverified`. The pipeline captures the
applied config revision when admitting a run. The first authenticated assistant
content/final event for that run performs a revision-CAS to `verified`; a
classified provider authentication/model rejection writes `failed` with a
stable reason. Do not issue a separate paid probe.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_provider_verification.py \
  apps/control-plane/tests/test_agent_pipeline.py
git diff --check
```

### Task 10: Fence Every Executing Path and Fresh-Gate Approvals

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_approvals.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/terminal_ports.py`
- Modify: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/pipeline.py`
- Modify: `apps/control-plane/tests/test_agent_approvals.py`
- Modify: `apps/control-plane/tests/test_agent_epoch_invalidation.py`
- Modify: `apps/control-plane/tests/test_agent_pipeline.py`

- [ ] **Step 1: Write stale-auth and stale-runtime RED tests**

Create tests named `test_stale_approve_returns_428_but_deny_remains_available`,
`test_runtime_epoch_change_after_approval_blocks_execution`, and
`test_observed_not_ready_blocks_submit_even_when_desired_enabled`. Assert no
gateway write or backend submit occurs in all rejected cases and denial still
reaches its terminal state under stale authentication.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_approvals.py \
  apps/control-plane/tests/test_agent_epoch_invalidation.py \
  apps/control-plane/tests/test_agent_pipeline.py
```

- [ ] **Step 3: Add the two gates**

Approval decides with `AdminAuthContext`; approve calls the freshness validator,
while deny does not. Command and pipeline final preflight reload desired Binding,
observed row, current disclosure, Pane Policy, revision, and epoch immediately
before dispatch. Use stable 428/409/503 codes from the spec.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_approvals.py \
  apps/control-plane/tests/test_agent_epoch_invalidation.py \
  apps/control-plane/tests/test_agent_pipeline.py \
  apps/control-plane/tests/test_mcp_server.py
git diff --check
```

### Task 11: Runtime Slice Verification and Handoff

**Files:**

- Verify only; do not change unrelated files.

- [ ] **Step 1: Run the runtime/auth slice**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_profile_config.py \
  apps/control-plane/tests/test_opencode_adapter.py \
  apps/control-plane/tests/test_agent_runtime_state_migration.py \
  apps/control-plane/tests/test_agent_runtime_state_repository.py \
  apps/control-plane/tests/test_agent_provider_disclosure.py \
  apps/control-plane/tests/test_auth_freshness.py \
  apps/control-plane/tests/test_http_runtime_client.py \
  apps/control-plane/tests/test_agent_runtime_registry.py \
  apps/control-plane/tests/test_agent_runtime_controller.py \
  apps/control-plane/tests/test_agent_provider_verification.py \
  apps/control-plane/tests/test_agent_approvals.py \
  apps/control-plane/tests/test_agent_epoch_invalidation.py \
  apps/control-plane/tests/test_agent_pipeline.py \
  apps/control-plane/tests/test_app_wiring.py
```

- [ ] **Step 2: Run static checks on touched Python**

```bash
.venv/bin/ruff check apps/control-plane/src apps/control-plane/tests
.venv/bin/mypy apps/control-plane/src
git diff --check
```

- [ ] **Step 3: Record handoff evidence**

Record exact exit codes, passed/failed counts, and remaining failures. Do not
call the slice complete unless both commands above exit zero and the spec and
quality reviewers approve the diff.
