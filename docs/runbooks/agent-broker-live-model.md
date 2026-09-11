# Agent Broker live-model verification runbook

This is a manual, deployment-only gate for the v0.2.0 Agent Broker. It is
separate from static contracts, the LLM-free OpenCode smoke test, and the
disposable proxy checks: a result is evidence for one recorded deployment, not
a general provider guarantee.

## Current status (2026-09-11)

The fixed `termflow-v020-local` project now runs the live profile with the
operator's DeepSeek credential and operator-supplied disclosure fields. A real
`deepseek-flash` turn completed the B → OpenCode → DeepSeek → approval →
Docker A `echo 1` path: one `send_text` approval was created and consumed, a
duplicate decision returned HTTP 409 `approval_already_decided`, Docker A
showed exactly one `echo 1` command and one standalone `1`, the final
assistant body reported `1`, and provider readiness moved to `verified` for
config revision 1. Live testing found and fixed two runtime integration
defects: the dispatcher now passes the active B conversation id to the model
as a trusted system part (every `termflow_*` write/watch tool requires it), and
the readiness probe now scopes OpenCode `/mcp` to the configured session
directory (an unscoped probe reported `failed` while the directory-scoped MCP
server was `connected`).

These disclosure fields are still operator-supplied and are **not**
independently verified policy evidence, so the stable-release gate remains
unverified. Do not set `no_training=true` without independent account/contract
evidence, and do not infer a policy value from the user-supplied endpoint or
token. The endpoint currently advertises `deepseek-flash` and
`deepseek-v4-pro`; `scripts/deploy/agent-local-preflight.sh` accepts
`deepseek-flash` and the legacy reference id `deepseek-v4-flash`.

## Preconditions

- Use the fixed local project `termflow-v020-local` only when its three durable
  volumes are the intended deployment volumes:
  `termflow-v020-local-data`, `termflow-v020-local-totp-key`, and
  `termflow-v020-local-opencode-data`. For an acceptance run, use a fresh
  project name and an override that gives every volume a unique project-owned
  physical name. Never point an opt-in fixture at the fixed volumes.
- Copy `.env.example` to the repository-root `.env`, generate every secret
  independently, and set mode `0600`. Run the non-mutating gate before any
  lifecycle command:

  ```bash
  chmod 0600 .env
  scripts/deploy/agent-local-preflight.sh --env-file "$PWD/.env"
  ```

  The preflight prints variable names and statuses only. It requires
  `TERMFLOW_ADMIN_TOKEN`, OpenCode basic-auth values, `OPENCODE_AGENT_MCP_TOKEN`,
  `DEEPSEEK_API_KEY`, the cleanup-helper token, and the complete
  `TERMFLOW_AGENT_PROVIDER_DEEPSEEK_*` policy fields.
- The bootstrap MCP token is one B-issued, binding-scoped capability installed
  identically in B and OpenCode. It is not the admin token, provider key, or
  cleanup-helper token. Do not place any raw value in this runbook, shell
  history, screenshots, URLs, or logs.
- The live overlay injects `DEEPSEEK_API_KEY` only into OpenCode and injects the
  provider catalog/disclosure fields into B. `credential_source` must be the
  literal source name `DEEPSEEK_API_KEY`; it is never a credential value.
- DeepSeek's public materials do not by themselves establish a universal
  `no_training` guarantee for this deployment. Set
  `TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING=true` only after the operator
  has account/contract evidence for the exact service and records that evidence
  outside this repository. Otherwise leave the sample blank and stop: the
  preflight and ProviderCatalog intentionally fail closed.
- Confirm the binding has a server-assigned `runtime_ref`, `runtime_epoch`, and
  `capability_ref`. Run the label-based inspector against the same project
  before testing behavior; it never calls Compose or mutates resources.

## Start and topology gate

After preflight succeeds, render and then start the live profile without
recreating or deleting durable volumes:

```bash
docker compose -p termflow-v020-local --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml config --quiet
docker compose -p termflow-v020-local --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml up -d --build
scripts/security/verify-agent-containers.sh --mode live termflow-v020-local
```

The expected path is:

```text
B -- agent_internal -- OpenCode -- provider_egress -- proxy -- provider_uplink
```

Only the proxy may join `provider_uplink`; OpenCode must not join the default
network or the uplink. The default network is B's normal host-facing bridge so
Docker can provide the explicit loopback publish used by Web C and the A/C API.
Docker A is deployed separately; on the same host it may join the exact
`<compose-project>_default` bridge and use `http://control-plane:8000`, while a
separate host uses B's canonical HTTPS/WSS origin (the local runbook's
"Docker A same-host fixture" has the exact build, enrollment, and run
commands). Its placement does not turn
B's client-facing network into an internal Docker network; do not create that
bridge with `--internal`. No OpenCode, proxy, or MCP port is published. The inspector checks full container
IDs (`docker ps -aq --no-trunc`), exact Compose labels, source paths of read-only
config mounts, fixed OpenCode volume ownership, image digests, non-root
identities, capabilities, limits, and forbidden B-secret names.

## Scenario

1. Record only UTC time, project name, image digests, non-secret resource IDs,
   git commit, inspector result, and health/status codes. Do not capture
   environment dumps or provider URLs with credentials.
2. Create one Agent Conversation through the admin/product API and open its
   Agent SSE stream. Save the initial opaque cursor.
3. Submit a bounded prompt asking the model for exactly one reviewed
   `termflow_*` read operation. Correlate the B timeline, OpenCode event, and
   MCP request using IDs only; verify no unreviewed built-in tool is offered.
4. Repeat with a write proposal in a disposable Term. Confirm B creates the
   canonical arguments hash and approval before any pane input. A stale
   authentication attempt must return the documented re-authentication error.
5. Approve exactly once, verify one A command and one execution receipt, then
   replay from the saved cursor. A duplicate decision or duplicate delivery
   must not produce a second write.
6. Revoke the binding or advance its auth epoch during another run. Confirm
   the existing SSE closes with the documented code and stale capabilities are
   rejected. Disconnect/reconnect OpenCode and confirm bounded reconciliation
   before dispatch resumes.
7. Delete the conversation/binding. Confirm active runs are cancelled, the
   backend session is deleted, the cleanup tombstone reaches `completed`, and
   only documented redacted retention metadata remains.

## Recording template

```text
date/time (UTC):
git commit:
compose project:
OpenCode image digest:
TermFlow image digest:
runtime_ref / epoch:
container-inspect evidence:
health/session/prompt/delete result:
MCP connected result:
read-tool event and cursor:
approval id/hash/decision and A receipt:
revocation close code and stale-token result:
deletion/tombstone result:
artifacts and redaction review:
operator / reviewer:
```

Any failed, skipped, or unrecorded step keeps the live-model gate
**unverified**. Never infer provider behavior from static tests, a proxy
configuration render, or an LLM-free OpenCode health probe.

## Fixed-project functional record (2026-09-11)

Project `termflow-v020-local` (main checkout `885bf96` plus the two live-path
fixes) ran the base + live Compose overlay with the pinned OpenCode image, the
allowlisted egress proxy, and Docker A pane `%0`. The operator supplied the
DeepSeek credential and disclosure fields; the endpoint advertised
`deepseek-flash`, and OpenCode was configured for it. Setup activated one
binding with runtime `ready` and provider `configured_unverified`. The real
model ("请在当前允许的终端中执行 echo 1，并告诉我结果。") proposed exactly one
`send_text`; B admitted the user message with HTTP 202, created one pending
approval, accepted the first decision with HTTP 200, rejected the duplicate
with HTTP 409 `approval_already_decided`, and finished with the approval
`consumed`. Docker A's pane contained exactly one `echo 1` command and one
standalone `1`; the single final assistant body reported `1`; provider
readiness advanced to `verified` for config revision 1. The live container
inspector passed. The disclosure fields remain operator-supplied, so this is
functional evidence only, not provider policy or stable-release acceptance.

## Disposable functional record (2026-09-09)

Project `termflow-v020-live-0909` used a fresh B image from the current
worktree, uniquely named volumes, a pinned OpenCode image, an allowlisted
provider-egress proxy, and Docker A pane `%0`. The real provider model created
one `send_text` proposal; B admitted the message with HTTP 202, created one
pending approval, accepted the first decision with HTTP 200, rejected the
duplicate with HTTP 409 `approval_already_decided`, and ended with the approval
`consumed`. The pane contained one command line `echo 1` and one standalone
output line `1`; the conversation contained one final assistant body exactly
`1`, with one `tool_started` and one `tool_completed` event.

After recreating B's host-facing network and B container and restarting
OpenCode, all three volume IDs were unchanged, runtime epoch remained 1, the
binding and conversation remained present, Docker A returned online, and the
runtime returned to `ready`. Host requests to `/healthz` and Web C returned
HTTP 200 on `127.0.0.1:48769`; the repository-root `.env` administrator token
authenticated successfully. The live security inspector passed. Provider
readiness remained `configured_unverified`, and the test-only disclosure fields
were synthetic, so this record must not be promoted to stable policy/release
acceptance.
