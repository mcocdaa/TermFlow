# OpenCode Reference Backend — Pinned Compatibility Contract

Status: **Pinned fixture** for TermFlow 0.2.0 Agent Broker milestone M0/M4.

This document is the pinned OpenCode reference contract for the direct HTTP/SSE
adapter (plan §6, §6.1, §10, §6.2.1). It records one selected SSE event mode, the
documented endpoint contract, the `directory` persistence rule, the MCP
handshake expectations, the "no exactly-once claim" note, and the ACP scope
declaration. The machine-readable contract is the embedded
`termflow-pin-contract` JSON block at the end; the prose below is the
human-readable contract and is the source of truth for that block.

The pin is corroborated by the captured OpenCode OpenAPI 3.1 spec
(`opencode-openapi.json`, captured from `https://dev.opencode.ai/openapi.json`)
and by the live server documentation (`https://dev.opencode.ai/docs/server/`).

---

## 1. Pinned SSE event mode (single selection)

**Pinned SSE event mode:** `/global/event` with `GlobalEvent.directory`

**Alternative SSE event mode:** `/event?directory=`

Exactly one mode is selected. The alternative is recorded for reference and is
NOT used by the 0.2.0 adapter.

Rationale (grounded in the captured OpenAPI spec):

- `GET /global/event` streams `GlobalEvent` envelopes whose payload is
  `{ directory, project?, workspace?, payload: Event }` — every event is
  self-describing with its `directory`, so one server-global SSE connection can
  serve every directory/session hosted by the runtime. B filters each envelope
  by the `directory` persisted in `BackendConversationRef` and the persisted
  `Session.id`, matching plan §6 ("`/global/event` with the returned
  `directory` and persisted `Session.id` used for filtering"). No reconnection
  is needed when a new session/directory is created on the same runtime.
- `GET /event?directory=` requires the `directory` at subscribe time and
  streams bare `Event` payloads (no per-event `directory`). It couples the SSE
  connection lifetime to one directory and requires a reconnect per directory
  change; plan §6 lists it as the project-scoped option, which is a reasonable
  mode but strictly more connection management for the same isolation.
- The reference runtime profile (§6.2.1) starts one OpenCode runtime per
  binding; `/global/event` covers that and also remains correct for a future
  fleet profile where a runtime hosts more than one directory.

`BackendConversationRef` persists the opaque OpenCode session ID plus the
`directory` returned by session creation. Every session-scoped request carries
and asserts that `directory` (see §3).

## 2. Documented endpoint contract (plan §6)

The adapter uses the documented contract. Endpoint shapes below are the pinned
contract; they match the captured OpenAPI 3.1 spec (path params use
`{sessionID}`/`:id` interchangeably in prose; the spec uses `{sessionID}`).

- `GET /global/health` — health and version; returns
  `{ healthy: true, version: string }` (spec: `global.health`).
- `POST /session` — create a session; body accepts `parentID` and `title`
  (plus optional `agent`, `model`, `metadata`, `permission`); the returned
  `Session.id` and `Session.directory` are recorded in `BackendConversationRef`.
  The spec also accepts an optional `directory` query parameter.
- `POST /session/:id/prompt_async` — asynchronous prompt; body requires
  `parts` (array of `TextPartInput | FilePartInput | AgentPartInput |
  SubtaskPartInput`) and may carry a client-supplied `messageID` (pattern
  `^msg`), `model`, `agent`, `noReply`, `tools`, `system`, `format`, `variant`.
  Returns `204 No Content` (no acknowledgement body).
- `GET /session/:id/message` — session message list for reconciliation; returns
  `{ info: Message, parts: Part[] }[]` (`limit` and `before` query params).
- `POST /session/:id/abort` — abort a running session; returns `boolean`.
- `POST /session/:id/permissions/:permissionID` — respond to a permission
  request; body `{ response: "once" | "always" | "reject" }` (required,
  `additionalProperties: false`); returns `boolean`. The live docs also mention
  a `remember` field, but the captured spec body is `{ response }` only; the
  spec is authoritative and `remember` is recorded as `unknown` (§7).
- `GET /session/status` — session status for all sessions; returns
  `{ [sessionID]: SessionStatus }` where `SessionStatus` is
  `idle | retry { attempt, message, next } | busy`.
- `GET /session/:id` — session details; returns `Session`.
- `DELETE /session/:id` — idempotent backend cleanup; returns `boolean`.
- `/doc` — the pinned contract fixture: the server publishes its OpenAPI 3.1
  spec at `/doc` (served as an HTML page with the spec, per the live docs). The
  captured machine-readable spec is frozen in `opencode-openapi.json` and is
  used by the adapter compatibility tests. `/doc` is the spec-serving endpoint,
  not an API operation, so it is not a path in the captured spec's `paths`.
- Server authentication: B authenticates its requests to the OpenCode server
  with the `OPENCODE_SERVER_PASSWORD` HTTP Basic credential supplied to the
  runtime profile (§6.2.1). It is a server-side transport credential for
  B→OpenCode requests and is **not** a replacement for `AgentToken`, which
  authenticates OpenCode→B MCP calls to the TermFlow MCP server.

## 3. `directory` persistence rule

Every session-scoped request carries and persists the `directory` returned by
session creation:

- `BackendConversationRef` stores the opaque OpenCode session ID **and** the
  OpenCode `directory` used to create and reconcile it (plan §6).
- The captured spec confirms that every session-scoped operation
  (`/session`, `/session/status`, `/session/{sessionID}`,
  `/session/{sessionID}/message`, `/session/{sessionID}/prompt_async`,
  `/session/{sessionID}/abort`,
  `/session/{sessionID}/permissions/{permissionID}`) accepts an optional
  `directory` query parameter. The adapter sends the persisted `directory` on
  every such request and asserts it on the response/event routing path.
- SSE events from `/global/event` carry `GlobalEvent.directory`; an event whose
  `directory` does not match the binding's persisted `directory` is dropped and
  recorded as a diagnostic, never routed to a different conversation.

## 4. MCP handshake expectations (plan §10)

MCP is the Agent-to-B transport facade: OpenCode is the MCP **client** and B
hosts the TermFlow MCP **server**. The pinned official SDK
(`mcp>=2.0,<3`, M0 commit `9d1aef8`) supplies transport/parser/handshake
primitives only. B explicitly configures and tests the following; **MCP
Streamable HTTP is never exposed to a browser**:

- Protocol revision — **dual-era baseline**: the pinned `mcp` SDK (2.0.0)
  speaks two protocol eras and the same `streamable_http_app` serves both:
  - Legacy era `2025-11-25` (MCP Streamable HTTP, plan §23 reference): the
    `initialize` handshake → server response → `notifications/initialized` →
    `tools/list` → per-call traffic; the server-issued `Mcp-Session-Id` is
    echoed on subsequent requests.
  - New era `2026-07-28`: no `initialize` handshake, no session, and no
    `Mcp-Session-Id`; the protocol version is carried per-request in `_meta`;
    server capability discovery uses `server/discover`; requests are routed
    with `Mcp-Method`/`Mcp-Name` headers.
  - **Decisive gate:** the revision actually negotiated by the pinned OpenCode
    image + pinned `mcp` SDK pair is verified by the M4 contract tests
    (§10.1); until M4 the negotiated era is `unknown`. B does not claim
    compatibility with every historical or draft revision.
- `Content-Type`: `application/json` on JSON requests (SSE responses use
  `text/event-stream`).
- `Accept`: `application/json, text/event-stream`.
- Host/Origin allowed values: B accepts MCP requests only from the pinned
  OpenCode runtime host/network alias on the deployment network; the concrete
  host value is pinned together with the M4 image and any drift fails closed.
  Because MCP Streamable HTTP is server-to-server here, `Origin` must be
  absent; requests carrying a browser-like `Origin` are rejected.
- Agent-token authentication: B accepts only a hashed `AgentToken` bound to one
  binding/Term/runtime epoch; no generic admin/native token fallback (§10).
  The `TokenVerifier` protocol (mcp SDK 2.0.0 auth, OAuth 2.1 resource server
  shape) is the implementation contract for AgentToken verification and is
  exercised by the M4 contract tests.
- Request IDs: JSON-RPC 2.0 `request.id` on every request, correlated by B, and
  never reused by the client.
- Session lifecycle: the legacy era keeps `initialize` → server response →
  `notifications/initialized` → `tools/list` with `Mcp-Session-Id` echoed; the
  new era uses per-request `_meta` versioning and `server/discover` with no
  session and no `Mcp-Session-Id`. Both eras end with the drain/quiesce
  sequence (§6.2.1).
- Notification behavior: client-to-server `notifications/initialized` (legacy
  era only) and `notifications/cancelled`; server-to-client
  `notifications/message`, `notifications/tools/list_changed`, and progress
  notifications are expected and handled; unknown notifications fail closed.

## 5. No exactly-once claim (plan §6.1)

`POST /session/:id/prompt_async` returns only `204 No Content` and does not
provide a transport-level idempotency key. The adapter therefore **must not
claim exactly-once delivery**. The guarantee is **"no silent duplicate
action"**: uncertain delivery is a visible, recoverable state.

- Persist an Inbox item before submitting; claim it with a compare-and-swap
  owner lease; mark the submission attempt before the network call.
- Submit with a B-generated `messageID` or equivalent stable backend key
  where the backend API permits (§6.1 step 3; the captured spec confirms
  `messageID` is a valid `prompt_async` body field).
- On SSE disconnect, reconnect and reconcile session messages/status.
- If B crashes after a prompt may have been accepted but before the response is
  known, mark the item `delivery_unknown`. Do not automatically resubmit an
  action-producing turn.

## 6. ACP scope declaration

**ACP is OUT of the 0.2.0 HTTP/SSE compatibility scope.** OpenCode ACP currently
launches `opencode acp` as a **stdio** subprocess (plan §6.2.1); remote ACP
transport is a separate bridge design and is not an assumption of this HTTP
adapter. A future ACP adapter must use the stdio/bridge contract, not assumed
remote ACP.

---

## 7. Pinned versioning and wire alignment

- **M4 exit image pin (2026-08-16, live):** `ghcr.io/anomalyco/opencode:1.18.18`
  digest `sha256:f3e00f8e25500150373c817e24b13f2f08e2ccd4cafd53dc3ad4827d47863b6f`
  (the official image — the upstream GitHub org is anomalyco/opencode).
  Live probes: `GET /global/health` → 200
  `{"healthy":true,"version":"1.18.18"}`; `POST /session` → 200 with
  `{id, slug, projectID:"global", directory:"/", version:"1.18.18", ...}`;
  `POST /session/:id/prompt_async` → 204; `POST /session/:id/abort` → 200;
  `DELETE /session/:id` → 200 `true`; `GET /global/event` SSE first frame
  `server.connected`; `GET /doc` OpenAPI paths **identical** to the pinned
  fixture (162/162, no additions/removals). The image ships the fixed
  `guest` UID **405** (no UID 1000/10001 user), busybox `wget`/`grep`
  (no node/curl), and requires `OPENCODE_SERVER_USERNAME`/
  `OPENCODE_SERVER_PASSWORD` basic auth on every endpoint when set (401
  otherwise). The `serve` subcommand starts the headless server
  (`opencode serve --hostname 0.0.0.0 --port 4096`).

- Image vs npm versioning: the OpenCode container image tags
  (`ghcr.io/anomalyco/opencode`) track the opencode CLI release series
  (`1.18.x`) — the same series as the `opencode-ai` npm package. The M4 exit
  image pin records tag `1.18.18`. The digest observed at research time
  (`sha256:531d22f5...`, 2026-08-12 capture) is superseded by the M4 exit
  digest (`sha256:f3e00f8e...`, 2026-08-16 live capture, above) and is
  recorded for traceability only; a deployment never assumes an image digest
  from this note.
- `/api/*` namespace (undocumented): the captured spec also contains an
  undocumented `/api/*` namespace (51 paths, including
  `/api/session/{id}/event?after=` durable event replay). It is OUT of the
  0.2.0 pinned contract; it is recorded here as a future reconciliation option
  if a later milestone needs replay beyond the documented endpoints.
- sse-starlette alignment: `sse-starlette` (BSD-3-Clause) is already a direct
  dependency of `mcp==2.0.0` (`>=3.0.0`); B aligns on `>=3.4,<4`, which adds
  zero marginal dependency when the Agent SSE response surface ships. Disconnect
  detection and bounded channels are confirmed for the aligned range.

---

## 8. Recorded unknowns and discrepancies

- `remember` field on the permission response: the live docs mention
  `{ response, remember? }`, but the captured OpenAPI spec defines the body as
  `{ response }` with `additionalProperties: false`. The spec is authoritative;
  `remember` is `unknown` and `always`/remember decisions are rejected anyway.
- The concrete MCP `Host` value and the negotiated MCP protocol revision are
  pinned together with the M4 image; until then they are `unknown`.
- Runtime behavior (event shapes on the wire, exact SSE framing) is verified by
  the M4 live-container contract tests; this pin records the documented
  contract.

---

```json termflow-pin-contract
{
  "backend": "opencode",
  "selected_event_mode": "/global/event",
  "selected_event_mode_rationale": "GET /global/event streams GlobalEvent envelopes carrying {directory, payload}; one connection serves every directory and B filters by the persisted directory and Session.id. The project-scoped /event?directory= alternative couples the stream to one directory and requires reconnects.",
  "alternative_event_mode": "/event?directory=",
  "endpoints": {
    "health": "GET /global/health",
    "session_create": "POST /session",
    "prompt_async": "POST /session/:id/prompt_async",
    "message": "GET /session/:id/message",
    "abort": "POST /session/:id/abort",
    "permissions": "POST /session/:id/permissions/:permissionID",
    "status": "GET /session/status",
    "session_get": "GET /session/:id",
    "session_delete": "DELETE /session/:id",
    "doc": "/doc"
  },
  "directory_persistence_rule": "BackendConversationRef persists the opaque OpenCode session id plus the directory returned by session creation; every session-scoped request (create, prompt_async, message, abort, permissions, status, get, delete) carries and asserts that directory as a query parameter, and /global/event envelopes are filtered by GlobalEvent.directory.",
  "no_exactly_once": {
    "prompt_async_status": "204",
    "transport_idempotency_key": "none",
    "guarantee": "no silent duplicate action; uncertain delivery is a visible recoverable state (delivery_unknown)",
    "note": "prompt_async accepts a client-supplied messageID body field usable for reconciliation but provides no transport-level idempotency key; exactly-once delivery is never claimed."
  },
  "mcp_handshake": {
    "protocol_revision": "dual-era: legacy 2025-11-25 (initialize handshake, session, Mcp-Session-Id) and new 2026-07-28 (no handshake/session/Mcp-Session-Id; per-request _meta versioning; server/discover; Mcp-Method/Mcp-Name headers); the revision actually negotiated by the pinned OpenCode image + mcp SDK (>=2.0,<3) is verified by the M4 contract tests and is the decisive gate; the negotiated era is unknown until then",
    "content_type": "application/json",
    "accept": "application/json, text/event-stream",
    "host_allowed": "pinned OpenCode runtime host/network alias on the deployment network; concrete value pinned with the M4 image; drift fails closed",
    "origin_allowed": "absent (server-to-server only, never exposed to a browser); browser-like Origin rejected",
    "request_ids": "JSON-RPC 2.0 request id on every request, correlated by B, never reused",
    "session_lifecycle": "legacy era: initialize, notifications/initialized, tools/list, Mcp-Session-Id echoed; new era: no session, per-request _meta versioning, server/discover, no Mcp-Session-Id; both eras: drain/quiesce on shutdown",
    "notification_behavior": "client sends notifications/initialized (legacy era only) and notifications/cancelled; server notifications/message and tools/list_changed expected; unknown notifications fail closed",
    "token_verifier": "TokenVerifier protocol (mcp SDK 2.0.0 auth, OAuth 2.1 resource server shape) is the implementation contract for AgentToken verification; exercised by the M4 contract tests"
  },
  "server_password": "OPENCODE_SERVER_PASSWORD HTTP Basic credential authenticates B-to-OpenCode server requests; not a replacement for AgentToken (OpenCode-to-B MCP auth)",
  "api_namespace_out_of_scope": "undocumented /api/* namespace (51 paths, incl. /api/session/{id}/event?after= durable replay) is out of the 0.2.0 pinned contract; recorded as a future reconciliation option",
  "versioning": {
    "image_sequence": "ghcr.io/anomalyco/opencode image tags track the opencode CLI release series (1.18.x); the M4 exit image pin records tag 1.18.18",
    "npm_sequence": "opencode-ai npm package follows the same 1.18.x release series",
    "digest": "M4 exit image digest sha256:f3e00f8e... (2026-08-16 live capture); the earlier research capture sha256:531d22f5... (2026-08-12) is superseded and kept for traceability only, never assumed for deployment"
  },
  "sse_starlette_alignment": ">=3.4,<4 (BSD-3-Clause); already a direct dependency of mcp==2.0.0 (>=3.0.0), zero marginal dependency; disconnect detection and bounded channels confirmed",
  "acp_out_of_scope": "ACP is out of scope for the 0.2.0 HTTP/SSE compatibility surface; a future ACP adapter must use the stdio/bridge contract, not assumed remote ACP."
}
```
