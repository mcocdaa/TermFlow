# TermFlow v0.2.0 Productized Agent Completion Design

**Status:** Approved in chat on 2026-09-04. This document turns the approved
"release-focused, single reference runtime" approach into an auditable design.

## 1. Goal

Complete the remaining v0.2.0 Agent Broker product path so an administrator can
open a Term in Web C, enable its Agent from the product UI, converse beside the
live terminal, approve a proposed write after fresh re-authentication, and see a
real Docker A execute `echo 1` through B and the pinned OpenCode runtime.

Completion also requires truthful runtime readiness, external-provider consent,
a durable local deployment, provider egress confinement, fail-closed startup,
and deletion receipts. A green static suite or an isolated OpenCode health probe
is evidence for only that layer and is not release completion.

## 2. Release Boundary

### 2.1 Included

- A responsive Agent sidecar embedded in the terminal page.
- A product setup flow for Profile, exact Pane Policy, provider disclosure, and
  Binding activation.
- Runtime activation and reconfiguration without restarting B.
- Separate desired Binding state and persisted observed runtime readiness.
- Fresh re-authentication for approvals and other sensitive Agent actions.
- A supported local deployment containing B, Web C, one pinned OpenCode
  reference runtime, a provider allowlist proxy, and persistent named volumes.
- Per-artifact cleanup receipts and visible `deletion_pending` semantics.
- Fail-closed Agent startup/recovery while preserving non-Agent terminal service.
- Deterministic browser coverage and a separate live DeepSeek `echo 1` record.

### 2.2 Not included

- A general Docker/Kubernetes runtime orchestrator.
- Product UI for creating arbitrary runtime containers or exposing raw MCP or
  provider credentials.
- More than one supported OpenCode runtime slot in the reference Compose
  deployment. Domain/API isolation remains binding-scoped so a future external
  supervisor can manage a fleet.
- Resizable sidecar panes, arbitrary JSON Profile editing, delegated write
  grants, or approval decisions that permanently allow future writes.
- Claiming an external provider is live merely because its configuration exists.

## 3. System Boundary and Authority

```text
Web C / Tauri shared UI
  | authenticated product APIs and Agent stream
  v
B Control Plane
  |-- desired Binding/Profile/Pane Policy/provider consent
  |-- runtime controller and observed readiness
  |-- approval policy, fresh-auth gate, cleanup coordinator
  |-- Agent pipeline and OpenCode adapter
  |
  +-- agent_internal (internal network) --> OpenCode
  |                                         |
  |                                         +-- provider_egress (internal)
  |                                               |
  |                                               v
  |                                       allowlist CONNECT proxy
  |                                               |
  |                                               v
  |                                       provider_uplink --> DeepSeek
  |
  +-- existing authenticated protocol --> Docker A --> tmux pane
```

B remains the sole authorization authority. OpenCode may request a terminal
operation but cannot authorize it. The browser never receives a provider key,
raw MCP token, root admin token, or TOTP master key. B never receives a Docker
socket. Deployment-owned cleanup is confirmed through narrow receipt APIs.

## 4. Domain and Persistence

### 4.1 Canonical Profile configuration

`AgentProfile.config` becomes validated canonical JSON rather than an opaque
string. The v0.2.0 schema contains exactly:

```json
{
  "provider_id": "deepseek",
  "model_id": "deepseek-v4-flash"
}
```

The provider endpoint, region, retention terms, no-training assertion, and
credential source come from a server-owned deployment catalog. A client cannot
submit an endpoint or credential. The OpenCode adapter sends the selected
`providerID` and `modelID` with every prompt so Profile configuration has real
runtime effect.

### 4.2 Desired Binding state

`AgentBinding` expresses administrator intent only:

- `disabled`: configured but must not run; this is the creation default.
- `enabled`: controller should maintain a ready pipeline.
- `revoked`: terminal state; all capabilities are fenced.

The client cannot write `ready` or `runtime_epoch`. Existing `pending` rows
migrate to `disabled`; existing `ready` rows migrate to `enabled`. A partial
unique index permits at most one non-revoked Binding for a `(profile_id,
term_id)` pair.

The Binding keeps server-owned desired fields:

- `runtime_ref`
- `runtime_epoch`
- `capability_ref`
- `config_revision`

`config_revision` changes for provider/model/runtime configuration. The
capability `runtime_epoch` changes only when authority must be invalidated:
revoke, runtime replacement, or capability rotation. A normal B restart and a
model-only configuration update do not rotate the epoch.

### 4.3 Observed runtime state

`AgentRuntimeBinding` has `UNIQUE(binding_id)` and records what the controller
has actually applied:

- `readiness`: `unprovisioned | reconciling | ready | not_ready | blocked |
  disabled`
- `reason_code`
- `observed_runtime_ref`
- `observed_runtime_epoch`
- `applied_revision`
- `config_fingerprint`
- `last_health_at`
- `transition_started_at`
- `provider_readiness`: `configured_unverified | verified | failed`
- `provider_verified_revision`
- `provider_last_checked_at`
- `provider_reason_code`
- timestamps

Only the runtime controller writes observed state. `ready` means all of the
following are true:

1. OpenCode answers authenticated `/global/health`.
2. Its `/mcp` state reports the TermFlow MCP server connected.
3. Profile configuration and provider-disclosure fingerprint match the desired
   revision.
4. The new adapter and Agent pipeline started successfully.
5. A final revision compare-and-swap succeeds before the mapping is published.

Provider verification is separate:

- `configured_unverified`: configuration is valid, but no paid prompt has
  proved the provider path.
- `verified`: a bounded live call succeeded during the current configuration
  revision.
- `failed`: the latest bounded verification failed, with a stable public reason.

Runtime `ready` must never be presented as proof that DeepSeek accepted a live
request.

### 4.4 Provider disclosure acceptance

Add an immutable `agent_provider_disclosure_acceptances` history table with:

- Binding ID
- disclosure fingerprint
- provider ID, model ID, endpoint origin, region
- retention terms/version, no-training value, policy version
- acceptance time and authentication epoch
- actor kind/reference and optional revocation time

The fingerprint is calculated over canonical server-owned disclosure data.
Changing the Profile, provider catalog, endpoint, policy version, retention, or
no-training value invalidates the acceptance and moves the Binding to `blocked`
with `binding_disclosure_stale`.

The existing v0.2.0 contract requires a complete disclosure, explicit consent,
the current policy version, and `no_training=true`. A deployment catalog may set
that flag only from verified provider policy supplied by the deployment owner;
unknown or false remains fail-closed. No provider credential is persisted in
this table.

### 4.5 Fresh authentication context

Authentication dependencies return an `AdminAuthContext`:

```text
credential_kind
actor_ref
auth_epoch
authenticated_at
```

Freshness defaults to 300 seconds and is checked against the original strong
authentication time:

- Browser sessions record it when root credential plus required TOTP succeeds.
- CLI access tokens inherit it from their strong-auth issuance.
- Native access/refresh tokens inherit it from the OAuth authorization approval.
- Refresh rotation preserves the original time and cannot manufacture freshness.
- A raw root bearer has no sensitive-action freshness and must first establish a
  Browser, CLI, or forced native authorization session.

Browser-session freshness stays in the existing bounded in-memory session
record. `AuthToken.authenticated_at` is persisted so CLI/native refresh and B
restart cannot accidentally renew or erase the original strong-auth time;
native issuance derives it from the existing OAuth `approved_at` value.

Approving a write, activating a Binding, accepting a provider disclosure, and
changing runtime authority require fresh authentication. Deny, disable, and
revoke remain available without step-up so a user can always make the system
safer. Approval request bodies continue to contain only the decision; passwords
and TOTP values never enter Agent payloads or the Tauri WebView.

### 4.6 Cleanup receipts

`AgentCleanupJob` is the durable aggregate. Add `AgentCleanupReceipt` children:

```text
cleanup_job_id
artifact_kind
artifact_ref
state: pending | confirmed | not_applicable | dead_letter
attempt_count
last_error
next_attempt_at
evidence_digest
policy_reason
policy_version
confirmed_at
created_at / updated_at
```

The unique key is `(cleanup_job_id, artifact_kind, artifact_ref)`. The manifest
covers B rows, watches, approvals, Agent tokens, backend sessions, runtime
attestation, runtime volume, container logs, SQLite WAL/backup, and provider
retention. `not_applicable` is legal only with a typed policy reason/version; a
missing handler can never imply completion.

A cleanup job completes only when every required receipt is `confirmed` or
validly `not_applicable`. `dead_letter` remains visible and never maps to
`completed`.

## 5. Application Services

### 5.1 Agent provisioning service

A focused `AgentProvisioningService` owns the product setup transaction. It
validates Term topology, Profile schema, exact Pane IDs, deployment bootstrap
secret availability, provider catalog, and disclosure fingerprint. It then
atomically creates or reuses:

- canonical Profile
- disabled/enabled desired Binding
- exact Pane Policy
- disclosure acceptance
- hash of the pre-deployed MCP capability token
- observed row in `reconciling`

The transaction commits before external runtime work. After commit it requests
an immediate bounded reconcile; startup and a periodic controller task provide
crash compensation. An idempotency key returns the same setup result rather than
creating duplicate Profiles, Bindings, tokens, or acceptances.

The reference deployment injects the same bootstrap MCP secret into B and
OpenCode before setup. The UI never sees it. If it is absent or does not match,
setup returns `deployment_required`. Capability epoch rotation intentionally
returns `capability_rotation_required` until the deployment operator installs a
new secret and recreates/reconfigures OpenCode; B itself does not restart.

### 5.2 Runtime controller

One `AgentRuntimeController` owns every runtime mutation. It uses a per-Binding
lock plus a global endpoint-isolation lock and follows this order:

1. Persist desired revision and mark observed `reconciling`; immediately fence
   new submits and tool calls.
2. If authority changes, advance epoch, revoke pending/approved approvals, and
   close affected streams.
3. Stop and unmap the previous pipeline; abort or reconcile active work.
4. Validate Profile, disclosure, Pane Policy, bootstrap capability, and runtime
   assignment.
5. Probe OpenCode health and connected MCP state.
6. Build and start a new pipeline without publishing it in the registry.
7. Re-read desired revision/fingerprint; publish only if they still match.
8. On failure, close the new adapter, leave the Binding unmapped, and persist a
   stable `not_ready` or `blocked` reason.

The controller reconciles after API mutations, at startup, and periodically.
Health drift immediately unmaps and fences the Binding. The registry must never
replace its live entry before the replacement pipeline has started.

Every command preflight, including the final check after human approval,
requires desired `enabled`, observed `ready`, current runtime epoch, matching
config revision, valid Pane Policy, and non-stale provider disclosure.

### 5.3 Cleanup coordinator

The coordinator creates a versioned receipt manifest before parent deletion and
reuses the same active job for repeated DELETE requests. B-owned receipts are
processed internally. Deployment-owned volume/log/container receipts are
confirmed by a narrow authenticated helper that sends only the receipt ID,
exact artifact reference, result, evidence digest, and idempotency key.

B is never given Docker access. The helper may inspect or remove only the exact
resource named by the receipt; unrelated containers and volumes are outside its
authority.

### 5.4 Startup coordinator

Agent startup stages are explicit:

1. DB migration/integrity
2. inbox, run, approval, and capability fencing
3. runtime-controller reconciliation
4. Docker A topology readiness/reconciliation
5. backend SSE reconciliation
6. watch rebuild
7. Agent pipeline and inbox-dispatch activation

A critical failure leaves Core terminal APIs and Web C running but marks Agent
Broker `degraded/recovery_failed`. In that state no Agent watch, pipeline, or
inbox dispatcher starts and no pending item is claimed. Cleanup retry may remain
active because it only reduces retained authority/data.

Database integrity and inability to establish the fencing ledger are critical.
An individual Docker A or OpenCode endpoint being offline is not a process-wide
critical failure: its Binding is persisted as `not_ready` while other bindings
and the terminal product remain available.

## 6. Product APIs

All endpoints remain under `/api/v1/agent` and use generated client contracts.

### 6.1 Setup

`GET /admin/setup?term_id=<uuid>` returns:

- `unconfigured | deployment_required | activating | ready | unavailable`
- Profile summary and server-selected runtime/provider/model preset
- desired Binding and observed runtime/provider states with stable reason codes
- token-installed boolean and expiry, never a raw token
- current exact Pane allowlist and topology revision
- disclosure fields, fingerprint, policy version, and acceptance state

`POST /admin/setup` accepts:

```json
{
  "term_id": "uuid",
  "profile_id": "optional-uuid",
  "profile_display_name": "optional-name",
  "pane_ids": ["%1"],
  "topology_revision": 17,
  "disclosure_fingerprint": "sha256",
  "accepted": true,
  "idempotency_key": "uuid"
}
```

Exactly one of `profile_id` and `profile_display_name` is present. Successful
commit returns `202 activating`; a bounded reconcile may return `200 ready` if
it finishes during the request. Missing deployment secret returns a non-secret
`409 deployment_required` response. Because this request records disclosure
consent and enables authority, it requires fresh authentication.

`topology_revision` is an integer because the existing canonical
`TopologySnapshot.revision` is numeric; setup does not introduce a second
string-encoded revision type.

### 6.2 Binding and policy

- `GET /admin/bindings/{id}` returns desired state, observed runtime state,
  provider-verification state, Pane Policy, and disclosure state.
- `POST /admin/bindings/{id}/activate` requires fresh auth and returns 202 while
  reconciling.
- `POST /admin/bindings/{id}/disable` fences synchronously, then stops runtime
  work asynchronously.
- `PUT /admin/bindings/{id}/runtime` accepts server-valid runtime/capability
  references plus `expected_revision`; clients cannot submit an epoch.
- `PUT /admin/bindings/{id}/pane-policies` replaces the exact allowlist after
  checking Binding ownership, Pane existence, and topology revision.
- `GET /admin/bindings/{id}/disclosure` returns current canonical disclosure.
- `POST /admin/bindings/{id}/disclosure/accept` requires the current fingerprint,
  `accepted:true`, and fresh auth.

The legacy PATCH may remain for compatibility but rejects client writes to
`ready`, `runtime_epoch`, observed state, and unknown Profile fields.

### 6.3 Approval and cleanup

- `POST /approvals/{id}/decide` returns HTTP 428 with
  `approval_reauthentication_required` when an approval is not fresh.
- Web establishes a fresh server-side session and retries the same decision at
  most once. Tauri performs forced native authorization without exposing root
  credentials to the WebView.
- DELETE returns 204 only if all required receipts reached terminal success
  synchronously. Otherwise it returns 202 with `cleanup_job_id`,
  `deletion_pending`, and `status_url`.
- `GET /admin/cleanup-jobs/{id}` exposes receipt states and safe reason codes.
- `POST /admin/cleanup-jobs/{job_id}/receipts/{receipt_id}/confirm` is the narrow
  deployment-helper acknowledgement endpoint.

## 7. Terminal Agent UI

### 7.1 Layout

- Desktop: a `26–30rem` right sidecar shares the terminal workspace. Existing
  terminal resize observation handles the reduced width without reconnecting.
- Narrow/mobile: the panel overlays the terminal workspace. Terminal canvas and
  MobileKeyBar become inert while open; the panel itself is not modal, leaving
  the approval `alertdialog` as the only modal surface.
- The Terminal title bar owns an Agent toggle with readiness and pending-approval
  badges. Closing restores focus to that toggle.
- v0.2.0 does not include drag resizing.

### 7.2 Component ownership

- `TerminalView` continues to own only terminal connection, viewport, and tmux
  operations; it owns sidecar visibility but no provisioning orchestration.
- `TerminalAgentPanel(termId)` selects setup, pending, unavailable, or chat
  content for exactly that Term.
- `useTermAgent` owns setup summary, Binding/readiness queries, conversation
  selection, activation retry, and error mapping.
- `AgentChatSession` becomes a single-root host-independent component with
  `variant="page|sidecar"`. Existing stream/history/composer state stays in
  `useAgentConversation`.
- `AgentSetupForm` implements Profile naming/selection, exact Pane selection,
  and explicit disclosure consent.
- Sidecar approvals use a collapsed native details tray; selecting a timeline
  approval opens and focuses the matching request.

### 7.3 Routing and privacy

Global Agent history remains at `/agent` and `/agent/:conversationId`. Embedded
chat uses `/terms/:termId?agent=<conversationId>`. Since the terminal route key
does not include the query, opening or changing conversations must not unmount
`TerminalView` or recreate its WebSocket.

Only the conversation ID enters the URL. Drafts, messages, terminal content,
tokens, credentials, provider keys, and disclosure details must not enter URL
parameters, local/session storage, or console logs. The mobile navigation grid
is corrected from three to four items when Agent navigation is enabled.

## 8. Reference Deployment

The repository provides a durable local profile rather than a `/tmp` audit
stack:

- Stable Compose project name: `termflow-v020-local`.
- Explicit named volumes for B data, TOTP state, and OpenCode data.
- Configuration bind mounts use long syntax with
  `bind.create_host_path:false`, so a missing file fails instead of becoming a
  directory.
- The root repository `.env` is passed explicitly with `--env-file` and must be
  mode `0600`. Documentation and scripts print variable names only, never values.
- The previous temporary project and volumes are preserved until the user
  separately authorizes exact cleanup targets.

Live provider topology uses two internal networks and one proxy uplink:

```text
B -- agent_internal -- OpenCode -- provider_egress -- proxy -- provider_uplink
```

Only the proxy has internet reachability. It permits CONNECT on port 443 only to
the configured provider hostname and rejects raw IPs, other domains, and other
ports. It has no host port and cannot join `agent_internal`. OpenCode receives
`HTTPS_PROXY`; `NO_PROXY` is limited to B, the runtime name, localhost, and
loopback. Proxy and OpenCode images are digest-pinned and run non-root with a
read-only root filesystem, dropped capabilities, no-new-privileges, tmpfs, and
resource limits.

Security verification discovers running resources by Compose labels rather than
re-rendering the original environment. It has separate offline and live topology
expectations and verifies that only the proxy owns the uplink.

## 9. Stable Error Semantics

Public APIs expose bounded codes, never raw URLs, credentials, headers, or
exceptions:

- 428 `approval_reauthentication_required`
- 428 `sensitive_action_reauthentication_required`
- 409 `deployment_required`
- 409 `binding_disclosure_required`
- 409 `binding_disclosure_stale`
- 409 `capability_rotation_required`
- 409 `binding_state_conflict`
- 409 `topology_revision_stale`
- 422 `invalid_profile_config`
- 422 `unknown_provider`
- 503 `binding_runtime_unavailable`

Observed runtime reasons include `runtime_unreachable`, `mcp_not_connected`,
`pipeline_start_failed`, `recovery_failed`, and `runtime_assignment_conflict`.

## 10. Migration and Compatibility

Migration numbers are serialized:

- `0011`: desired/observed runtime state, provider-verification fields, config
  revision, canonical provider disclosure acceptance, and
  `AuthToken.authenticated_at` needed for freshness.
- `0012`: cleanup job manifest fields and cleanup receipts.

Both migrations support empty-database upgrade, `0010 -> head`, downgrade, and
manifest ownership tests. Generated TypeScript contracts are regenerated only
from the Python contract source.

Old status compatibility is explicit: `pending -> disabled`, `ready -> enabled`,
`disabled -> disabled`, and `revoked -> revoked`. A migration must preserve
runtime references and epochs; it must not infer observed `ready` merely from an
old desired string.

This design supersedes two stale statements in the original plan:

1. Plain B restart does not rotate a runtime epoch.
2. Authenticated `/global/health` alone is insufficient for product readiness;
   connected MCP and a started/published pipeline are also required.

## 11. Verification Contract

Every behavioral change follows red-green-refactor. Release evidence is kept in
separate tiers:

1. Unit/contract: migrations, repositories, controller races, registry rollback,
   disclosure drift, auth freshness, cleanup receipts, client parsing, UI states,
   Compose rendering, and proxy policy.
2. Deterministic integration: real B process, fake OpenCode/provider, dynamic
   activation without B restart, recovery failure, health drift, approval
   re-auth, and deletion retry.
3. Browser: login, open Term, open sidecar, setup, select Pane, accept disclosure,
   converse, approve, observe terminal result, reload, responsive layout, focus,
   and proof that the terminal WebSocket was not recreated.
4. Container: pinned OpenCode reports TermFlow MCP connected; persistent volumes
   survive B/OpenCode restarts; proxy allow/deny checks pass; container inspection
   proves the declared isolation.
5. Live external provider: with Docker A and user-supplied DeepSeek credentials,
   ask the Agent to run `echo 1`, approve after fresh auth, observe exactly one A
   receipt and terminal output `1`, then record model/config revision and safe
   evidence without the key or prompt contents.
6. Adversarial lifecycle: stale token/epoch, wrong host, revoke during a turn,
   disconnect/reconcile, repeated approval, repeated DELETE, cleanup dependency
   outage/recovery, and dead-letter visibility.
7. Full repository verification: Python, npm, generated contracts, Rust, Tauri
   no-bundle build, Compose/security checks, and Control Plane image build.

M4/M8 checkboxes and v0.2.0 release notes may be updated only after their exact
dynamic gates have fresh evidence. No tag, merge, push, or old-resource deletion
is part of this implementation unless separately requested.

## 12. Delivery Slices and File Ownership

Implementation is split into ordered, independently reviewed slices:

1. Runtime/disclosure/auth model and controller (`0011`). One runtime integration
   owner exclusively edits the current backend conflict hotspots.
2. Product setup API and generated contracts.
3. Terminal sidecar and setup UI. One UI owner exclusively edits shared client
   files after API shapes are frozen.
4. Cleanup coordinator and receipts (`0012`). It follows `0011` and does not race
   for the Alembic head.
5. Startup degradation and durable deployment/provider proxy.
6. Deterministic/browser/container/live acceptance and documentation bookkeeping.

Fresh implementer agents follow the written tasks with test-first proof. Each
slice receives a separate specification-compliance review and code-quality
review before the next overlapping slice begins. Read-only review and independent
test investigation may run in parallel; implementation agents never concurrently
edit the same dirty backend files.
