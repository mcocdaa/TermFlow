# OpenCode Reference Backend — Capability Matrix (M0 pin)

This fixture records the **documented** capability matrix for the OpenCode
reference backend adapter (plan §6, §6.1, §6.2.1, §10). Every dimension below
is a documented contract expectation. Nothing here is verified against a live
OpenCode container yet: live-container contract tests happen at M4, and until
they pass every dimension carries `verification_status: "unknown"` (or
`"unsupported"`). Unverified behavior is never silently marked "yes".

The matrix records the **shipped v0.2.0 adapter semantics**: values here match
the adapter's frozen capability descriptor (the descriptor a test can pin).
Where the plan cites a later target semantic that the shipped adapter does not
yet implement (e.g. volume-proof resume, §6.2.1 attribution), the target is
recorded as `plan_target` alongside the shipped value — it is never asserted
as already shipped.

The machine-readable matrix is the embedded `termflow-capability-matrix`
JSON block; the tables below are the human-readable form.

## 1. Accepted AgentInput kinds (plan §6 mapping)

| AgentInput kind | Mapping | Verification |
| --- | --- | --- |
| `user_message` | `supported` — normal user text part | `unknown` (M4) |
| `watch_triggered` | `supported` — user-visible input part carrying a structured TermFlow event; terminal observation explicitly untrusted, never a developer/system instruction | `unknown` (M4) |
| `permission_resolved` | `supported` via the optional `BackendInteraction` facet — resolved through `POST /session/:id/permissions/:permissionID`; provider permission state is never promoted into B terminal authority | `unknown` (M4) |
| `timer_triggered` | `supported` — bounded, user-visible typed text/event part | `unknown` (M4) |
| `system_notification` | `supported` — bounded, user-visible typed text/event part | `unknown` (M4) |
| any other kind | `unsupported` — remains queued and fails closed, emitting a canonical `RunFailed` or diagnostic event; never silently coerced | `unknown` (M4) |

## 2. Event dedup identity (plan §6.1 step 7)

Deduplication uses backend event/message/part IDs plus B's run mapping. The
captured OpenAPI spec confirms the identity components available on the wire.

| Component | Source | Verification |
| --- | --- | --- |
| `directory` | `GlobalEvent.directory` (every `/global/event` envelope) | `unknown` (M4) |
| event `id` | SSE event payload `id` (pattern `^evt_`) | `unknown` (M4) |
| message `id` | `Message.id` (pattern `^msg`) | `unknown` (M4) |
| part `id` | `Part.id` (pattern `^prt`) | `unknown` (M4) |
| permission `id` | `PermissionRequest.id` / permission events (pattern `^per`) | `unknown` (M4) |
| session `id` | `Session.id` (pattern `^ses`) | `unknown` (M4) |
| B run mapping | B-side mapping from canonical run to backend conversation/message IDs | `unknown` (M4) |

## 3. Submit idempotency (plan §6.1)

| Property | Value | Verification |
| --- | --- | --- |
| `mode` | `non_idempotent` | `unknown` (M4) |
| `prompt_async` response | `204 No Content` | `unknown` (M4) |
| transport idempotency key | `none` | `unknown` (M4) |
| client-supplied `messageID` body field | available for reconciliation, not an idempotency guarantee | `unknown` (M4) |
| exactly-once claim | never made — "no silent duplicate action"; uncertain delivery is `delivery_unknown`, a visible recoverable state | `unknown` (M4) |

## 4. Run-boundary inference

| Property | Value | Verification |
| --- | --- | --- |
| `mode` | `inferred` — boundaries inferred from `SessionStatus` (`idle`/`busy`/`retry`) transitions and message/step events (`session.status`, `session.idle`, `message.updated`, `step-start`, `step-finish`) | `unknown` (M4) |
| explicit backend run id | `none` — B tracks runs via its own canonical run mapping | `unknown` (M4) |

## 5. Cancel semantics

| Property | Value | Verification |
| --- | --- | --- |
| `scope` | `conversation` — `POST /session/:id/abort` aborts the running session (the in-flight message); there is no finer per-run cancel endpoint | `unknown` (M4) |
| `endpoint` | `POST /session/:id/abort` (returns `boolean`) | `unknown` (M4) |
| fail-closed | cancel during drain/epoch rotation leaves the old run `unknown` and blocks activation of the new conversation (§6.2.1) | `unknown` (M4) |

## 6. Resume / delete

| Property | Value | Verification |
| --- | --- | --- |
| `context_mode` | `lost_on_restart` — shipped v0.2.0 semantic: `reconcile` never returns `resumable=True`; a 404 session yields `CONTEXT_LOST`. Plan §6 targets `resume_requires_volume_proof` (resume only when the runtime persistence volume and health contract prove backend context survived restart); that upgrade is not yet implemented by the adapter and is recorded as `plan_target` | `unknown` (M4) |
| `context_lost_fallback` | `fork_from_curated_transcript` — B-side plan §6 fallback: the adapter reports `CONTEXT_LOST` and leaves the fork-from-curated-transcript decision to B | `unknown` (M4) |
| `delete_endpoint` | `DELETE /session/:id` — idempotent backend cleanup | `unknown` (M4) |

## 7. Runtime / conversation isolation (plan §6.2.1)

| Property | Value | Verification |
| --- | --- | --- |
| `runtime_isolation` | `binding` — one runtime/container is never shared across bindings unless an adapter capability explicitly proves per-binding tool isolation | `unknown` (M4) |
| `concurrency_mode` | `serialized` — one active Agent Run per binding; a second conversation waits in the Inbox | `unknown` (M4) |
| `tool_call_identity` | `binding` — shipped v0.2.0 semantic: backend tool-call `callID` values are server-generated and unique within the binding's runtime. The plan §6.2.1 active-run lease (a binding-scoped MCP call is accepted only while exactly one B-owned run is active and is attached to that run before policy evaluation) is the v0.2 tool-call **attribution** mechanism, implemented at the broker/MCP layer — it does not change the backend ID-uniqueness scope of the adapter descriptor | `unknown` (M4) |
| `one_active_run_per_binding` | `contractual` — required by the v0.2 safety default | `unknown` (M4) |
| `epoch_bound_mcp_capability` | `contractual` — freshly provisioned epoch-bound binding token; late old-epoch calls are rejected and recorded, never reassigned | `unknown` (M4) |

## 8. MCP protocol eras and wire alignment (plan §10, research 2026-08-12)

| Property | Value | Verification |
| --- | --- | --- |
| `mcp_protocol_eras` | `dual_era` — legacy `2025-11-25` (initialize handshake, session, `Mcp-Session-Id`) and new `2026-07-28` (no handshake/session/`Mcp-Session-Id`; per-request `_meta` versioning; `server/discover`; `Mcp-Method`/`Mcp-Name` headers); the same `streamable_http_app` serves both; `negotiation_gate`: `decisive` — the revision actually negotiated by the pinned OpenCode image + pinned `mcp` SDK pair is verified by the M4 contract tests, so the negotiated era is `unknown` until then | `unknown` (M4) |
| `api_namespace` | `out_of_scope` — the undocumented `/api/*` namespace (51 paths, incl. `/api/session/{id}/event?after=` durable replay) is recorded but not part of the 0.2.0 pinned contract; future reconciliation option | `unknown` (M4) |
| `sse_starlette_alignment` | `aligned` — `>=3.4,<4` (BSD-3-Clause), already a direct dependency of `mcp==2.0.0` (`>=3.0.0`), zero marginal dependency; disconnect detection and bounded channels documented | `unknown` (M4) |

---

```json termflow-capability-matrix
{
  "backend": "opencode",
  "matrix_version": "0.2.0-m0",
  "verification_note": "Documented contract only; live-container verification is M4. Every dimension carries verification_status unknown/unsupported until then; unverified items are never silently marked yes.",
  "dimensions": {
    "accepted_input_kinds": {
      "user_message": "supported",
      "watch_triggered": "supported",
      "permission_resolved": "supported_via_optional_backendinteraction",
      "timer_triggered": "supported",
      "system_notification": "supported",
      "unsupported_kind_behavior": "fail_closed",
      "verification_status": "unknown"
    },
    "event_dedup_identity": {
      "identity_components": [
        "directory",
        "event_id",
        "message_id",
        "part_id",
        "permission_id",
        "session_id",
        "run_mapping"
      ],
      "dedup_rule": "backend event/message/part IDs plus B run mapping (plan §6.1)",
      "verification_status": "unknown"
    },
    "submit_idempotency": {
      "mode": "non_idempotent",
      "prompt_async_status": "204",
      "transport_idempotency_key": "none",
      "message_id_reconciliation": "documented",
      "exactly_once_claim": "none",
      "verification_status": "unknown"
    },
    "run_boundary_inference": {
      "mode": "inferred_from_session_status_and_events",
      "explicit_run_id": "none",
      "evidence_sources": [
        "session.status",
        "session.idle",
        "message.updated",
        "step-start",
        "step-finish"
      ],
      "verification_status": "unknown"
    },
    "cancel": {
      "scope": "conversation",
      "endpoint": "POST /session/:id/abort",
      "finer_run_cancel": "unsupported",
      "verification_status": "unknown"
    },
    "resume_delete": {
      "context_mode": "lost_on_restart",
      "context_lost_fallback": "fork_from_curated_transcript",
      "delete_endpoint": "DELETE /session/:id",
      "plan_target": "resume_requires_volume_proof (plan §6): resume only when the runtime persistence volume and health contract prove backend context survived restart; not yet implemented by the shipped v0.2.0 adapter",
      "verification_status": "unknown"
    },
    "isolation": {
      "runtime_isolation": "binding",
      "concurrency_mode": "serialized",
      "tool_call_identity": "binding",
      "attribution_mechanism": "the active-run lease is the v0.2 tool-call attribution mechanism (plan §6.2.1), implemented at the broker/MCP layer; backend tool-call IDs are server-generated and unique within the binding runtime",
      "one_active_run_per_binding": "contractual",
      "epoch_bound_mcp_capability": "contractual",
      "late_old_epoch_call": "rejected_and_recorded",
      "verification_status": "unknown"
    },
    "mcp_protocol_eras": {
      "eras": ["2025-11-25", "2026-07-28"],
      "legacy_era": "initialize handshake, session, Mcp-Session-Id",
      "new_era": "no handshake/session/Mcp-Session-Id; per-request _meta versioning; server/discover; Mcp-Method/Mcp-Name headers",
      "server_serves_both": "the same streamable_http_app serves both eras (mcp SDK 2.0.0)",
      "negotiation_gate": "decisive - revision negotiated by the pinned OpenCode image + pinned mcp SDK pair verified by the M4 contract tests; negotiated era unknown until then",
      "verification_status": "unknown"
    },
    "api_namespace": {
      "scope": "out_of_0.2_pinned_contract",
      "paths_recorded": "undocumented /api/* namespace (51 paths, incl. /api/session/{id}/event?after= durable replay)",
      "reconciliation": "future option, not part of the 0.2.0 pinned contract",
      "verification_status": "unknown"
    },
    "sse_starlette_alignment": {
      "pin": ">=3.4,<4",
      "license": "BSD-3-Clause",
      "dependency_status": "already a direct dependency of mcp==2.0.0 (>=3.0.0); zero marginal dependency",
      "documented_features": "disconnect detection and bounded channels",
      "verification_status": "unknown"
    }
  }
}
```
