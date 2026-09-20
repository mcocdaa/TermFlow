# v0.2.0 Product Setup API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose one safe, idempotent product workflow that configures a Term Agent, accepts current provider disclosure, installs exact Pane Policy, and dynamically activates the Binding.

**Architecture:** `AgentProvisioningService` is the transaction boundary; HTTP handlers translate strict Pydantic requests to commands and trigger the runtime controller only after commit. Generated TypeScript contracts and `AgentsApi` mirror the aggregate setup/readiness view without exposing raw credentials.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, pytest, Python contract generator, TypeScript, Vitest, Tauri Rust HTTP transport.

---

**Dependency:** Complete and review `2026-09-04-v020-runtime-authority.md` first.

**Execution constraint:** Preserve dirty files and do not commit/push/reset/clean. Freeze all API types before the UI plan begins.

### Task 1: Implement Atomic `AgentProvisioningService`

**Files:**

- Create: `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/provisioning.py`
- Create: `apps/control-plane/tests/test_agent_provisioning.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`

- [ ] **Step 1: Write provisioning RED tests**

Create tests named `test_setup_is_atomic_and_idempotent`,
`test_setup_rejects_stale_topology_before_writing_any_rows`,
`test_setup_missing_bootstrap_secret_writes_nothing`, and
`test_reconcile_failure_keeps_consistent_activating_state`. Count rows in every
affected table before/after failures, repeat the same UUID idempotency key, and
assert reconcile failure leaves one enabled Binding plus one observed
not-ready/reconciling record rather than a partial duplicate.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_provisioning.py
```

Expected: missing service/types.

- [ ] **Step 3: Implement command/result and one transaction**

```python
@dataclass(frozen=True, slots=True)
class AgentSetupCommand:
    term_id: UUID
    profile_id: UUID | None
    profile_display_name: str | None
    pane_ids: tuple[str, ...]
    topology_revision: int
    disclosure_fingerprint: str
    accepted: bool
    idempotency_key: UUID


@dataclass(frozen=True, slots=True)
class AgentSetupResult:
    state: str
    term_id: UUID
    binding_id: UUID | None
    reason_code: str | None


class AgentProvisioningService:
    def __init__(
        self, repositories, sessions, topology, catalog, bootstrap_secret, controller
    ) -> None:
        self._repositories = repositories
        self._sessions = sessions
        self._topology = topology
        self._catalog = catalog
        self._bootstrap_secret = bootstrap_secret
        self._controller = controller
```

Add async `setup(command, auth) -> AgentSetupResult` and
`inspect(term_id) -> AgentSetupView` methods to this service.

Validate topology and deployment catalog first. In one database transaction,
create/reuse Profile, desired Binding, exact Pane Policy, disclosure acceptance,
bootstrap AgentToken hash with fixed observe/write scopes, idempotency receipt,
and observed `reconciling`. Commit, then invoke one bounded controller reconcile.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_provisioning.py \
  apps/control-plane/tests/test_agent_runtime_controller.py
git diff --check
```

### Task 2: Freeze Setup and Binding HTTP Contracts

**Files:**

- Create: `apps/control-plane/tests/test_agent_setup_api.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_admin.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`

- [ ] **Step 1: Write strict-model and response RED tests**

Create tests named `test_setup_request_requires_exactly_one_profile_selector`,
`test_setup_rejects_extra_fields_and_stale_topology`,
`test_setup_response_never_contains_raw_token_or_provider_key`, and
`test_setup_get_distinguishes_deployment_required_and_unavailable`. Assert exact
422/409/200/202 status codes and recursively inspect response keys/values for
forbidden secret fields.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_setup_api.py
```

- [ ] **Step 3: Add the aggregate Pydantic types**

```python
class AgentSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: UUID
    profile_id: UUID | None = None
    profile_display_name: str | None = Field(default=None, min_length=1, max_length=128)
    pane_ids: list[str] = Field(min_length=1)
    topology_revision: int = Field(ge=0)
    disclosure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted: Literal[True]
    idempotency_key: UUID


class AgentRuntimeStateResponse(BaseModel):
    readiness: str
    reason_code: str | None
    config_revision: int
    applied_revision: int | None
    runtime_epoch: int | None
    provider_readiness: str
```

Also define `AgentSetupResponse`, `AgentSetupProfileSummary`,
`AgentSetupTokenSummary`, `AgentBindingDetailResponse`,
`AgentPanePolicyResponse`, `AgentProviderDisclosureResponse`,
`AgentBindingRuntimeUpdateRequest`, `AgentPanePolicyReplaceRequest`, and
`AgentDisclosureAcceptRequest`, all with `extra="forbid"`.

Add `GET /admin/setup` and fresh-auth `POST /admin/setup`. Return 202 for
activating and 200 only for an already completed bounded reconcile.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_setup_api.py \
  apps/control-plane/tests/test_agent_provisioning.py
git diff --check
```

### Task 3: Add Runtime, Pane Policy, and Disclosure Mutations

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/api/agent_admin.py`
- Modify: `apps/control-plane/tests/test_agent_setup_api.py`
- Modify: `apps/control-plane/tests/test_agent_token_auth.py`
- Modify: `apps/control-plane/tests/test_mcp_server.py`

- [ ] **Step 1: Add endpoint RED tests**

Create tests named `test_legacy_patch_rejects_ready_epoch_and_observed_fields`,
`test_activate_requires_fresh_auth_and_returns_202`,
`test_disable_fences_before_returning`,
`test_replace_pane_policy_checks_term_panes_and_revision`, and
`test_accept_disclosure_rejects_stale_fingerprint`. Assert the controller mock
sees fencing before disable returns, rejected mutations leave revisions
unchanged, and no unknown Pane enters the repository.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_setup_api.py \
  apps/control-plane/tests/test_agent_token_auth.py \
  apps/control-plane/tests/test_mcp_server.py
```

- [ ] **Step 3: Implement product endpoints**

Implement exactly:

```text
GET  /admin/bindings/{id}
POST /admin/bindings/{id}/activate
POST /admin/bindings/{id}/disable
PUT  /admin/bindings/{id}/runtime
PUT  /admin/bindings/{id}/pane-policies
GET  /admin/bindings/{id}/disclosure
POST /admin/bindings/{id}/disclosure/accept
```

Runtime update uses `expected_revision` and never accepts an epoch. Pane policy
is a complete exact replacement and cannot enable `all_panes`. Activation,
runtime mutation, and disclosure acceptance require `require_fresh_admin`.
Disable/revoke use normal admin auth and synchronously fence before response.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_setup_api.py \
  apps/control-plane/tests/test_agent_token_auth.py \
  apps/control-plane/tests/test_mcp_server.py \
  apps/control-plane/tests/test_agent_epoch_invalidation.py
git diff --check
```

### Task 4: Generate Client Contracts

**Files:**

- Modify: `scripts/generate-client-contracts/generate.py`
- Modify: `packages/client-contracts/src/generated.ts` (generator output only)
- Modify: `packages/client-contracts/src/index.ts`
- Modify: `tests/contracts/test_client_contract_generation.py`

- [ ] **Step 1: Add generator RED assertions**

```python
def test_agent_setup_and_observed_runtime_contracts_are_generated() -> None:
    output = generated_contract_text()
    assert "export interface AgentSetupResponse" in output
    assert "provider_readiness" in output
    assert "raw_token" not in setup_response_block(output)
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/contracts/test_client_contract_generation.py
```

- [ ] **Step 3: Extend the generator and regenerate**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run contracts:generate
```

Never hand-edit generated property names. Verify request/response unions include
the exact setup states and stable public reason codes.

- [ ] **Step 4: Verify no drift**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run contracts:check
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/contracts/test_client_contract_generation.py
git diff --check
```

### Task 5: Extend `AgentsApi` and Native PUT Transport

**Files:**

- Create: `packages/client-core/src/api/agents.test.ts`
- Modify: `packages/client-core/src/api/agents.ts`
- Modify: `packages/client-core/src/http/types.ts`
- Modify: `packages/client-core/src/http/apiClient.test.ts`
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`
- Modify: `apps/clients/tauri/src-tauri/tests/agent_stream_contract.rs`

- [ ] **Step 1: Write request-shape RED tests**

```typescript
it('replaces pane policy with PUT and numeric topology revision', async () => {
  await api.replacePanePolicies('binding-1', {
    pane_ids: ['%1'],
    topology_revision: 17,
  })
  expect(request.method).toBe('PUT')
  expect(request.path).toBe('/api/v1/agent/admin/bindings/binding-1/pane-policies')
  expect(request.body).toEqual({ pane_ids: ['%1'], topology_revision: 17 })
})
```

Cover term/profile list filters, setup, activation, disable, runtime update, and
disclosure acceptance. Assert URL encoding and AbortSignal propagation.

- [ ] **Step 2: Verify RED**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- src/api/agents.test.ts
```

- [ ] **Step 3: Implement the API surface**

```typescript
export interface AgentsApi {
  listProfiles(signal?: AbortSignal): Promise<AgentProfileListResponse>
  getSetup(termId: string, signal?: AbortSignal): Promise<AgentSetupResponse>
  setup(body: AgentSetupRequest, signal?: AbortSignal): Promise<AgentSetupResponse>
  getBinding(bindingId: string, signal?: AbortSignal): Promise<AgentBindingDetailResponse>
  activateBinding(bindingId: string, signal?: AbortSignal): Promise<AgentBindingDetailResponse>
  disableBinding(bindingId: string, signal?: AbortSignal): Promise<AgentBindingDetailResponse>
  updateBindingRuntime(bindingId: string, body: AgentBindingRuntimeUpdateRequest, signal?: AbortSignal): Promise<AgentBindingDetailResponse>
  replacePanePolicies(bindingId: string, body: AgentPanePolicyReplaceRequest, signal?: AbortSignal): Promise<AgentPanePolicyResponse>
  getDisclosure(bindingId: string, signal?: AbortSignal): Promise<AgentProviderDisclosureResponse>
  acceptDisclosure(bindingId: string, body: AgentDisclosureAcceptRequest, signal?: AbortSignal): Promise<AgentProviderDisclosureResponse>
}
```

Add `PUT` to the shared `HttpMethod` and the Tauri native request match. Do not
fall back to POST tunnelling.

- [ ] **Step 4: Verify GREEN**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- \
  src/api/agents.test.ts src/http/apiClient.test.ts
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run typecheck --workspace @termflow/client-core
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
git diff --check
```

### Task 6: Product API Slice Verification

**Files:** Verify only.

- [ ] **Step 1: Run API and contract suite**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_agent_provisioning.py \
  apps/control-plane/tests/test_agent_setup_api.py \
  apps/control-plane/tests/test_agent_token_auth.py \
  apps/control-plane/tests/test_mcp_server.py \
  tests/contracts/test_client_contract_generation.py
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run contracts:check
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run test:run --workspace @termflow/client-core
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH npm run typecheck --workspace @termflow/client-core
git diff --check
```

- [ ] **Step 2: Record exact evidence**

Record exit codes and test counts. The API contract is frozen only after spec
and quality reviews approve it; then hand it to the UI owner.
