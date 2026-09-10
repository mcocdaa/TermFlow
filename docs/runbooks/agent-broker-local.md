# Agent Broker durable local deployment

This runbook owns the stable v0.2.0 B + Web C + OpenCode deployment. It does
not authorize cleanup of an older Compose project, container, network, or
volume; preserve those resources until their exact IDs are separately reviewed.

## Current gate status (2026-09-09)

The repository gate, deterministic product/lifecycle scenarios, browser
sidecar checks, and disposable OpenCode/provider-egress topology checks are
green. A disposable functional deployment named `termflow-v020-live-0909`
proved that B can publish Web C and its API on `127.0.0.1:48769`, use the
repository-root `.env` administrator token, retain its binding/conversation
state across B and OpenCode recreation, and recover the runtime with Docker A
online. Its provider disclosure metadata was synthetic and unverified, so it
is not stable-release policy evidence.

The fixed `termflow-v020-local` project is still not live: the repository-root
`.env` contains only `TERMFLOW_ADMIN_TOKEN` and lacks the other 13 required
deployment/policy fields, so `agent-local-preflight.sh` correctly fails closed.
The existing `termflow-v020-local-0903-*` resources were not changed or used as
a substitute for the stable deployment.

## Prepare without changing runtime state

From the repository root, create the deployment file once, generate distinct
random values for every secret placeholder, and restrict its permissions:

```bash
cp .env.example .env
chmod 0600 .env
scripts/deploy/agent-local-preflight.sh --env-file "$PWD/.env"
```

The policy fields deliberately remain incomplete in `.env.example`. Supply
`no_training=true` only with account/contract evidence for this exact provider
configuration. Without it, preflight failure is the correct fail-closed result.
The preflight performs reads and Compose rendering only and must never print
credential values.

## Start the stable project

The supported Agent live slice always includes the allowlist proxy:

```bash
docker compose -p termflow-v020-local --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml up -d --build
scripts/security/verify-agent-containers.sh --mode live termflow-v020-local
curl -fsS http://127.0.0.1:8765/healthz
```

The base Compose file remains useful for offline/static configuration checks,
but it is not evidence for provider or MCP readiness. The full dynamic sequence
is documented in [the live-model runbook](agent-broker-live-model.md).

The default Compose network is an ordinary bridge because B owns the explicit
loopback host publish used by Web C and the A/C API. Only `agent_internal`
(B to OpenCode) and the provider-egress segment are internal networks. Docker A
is deployed separately; its network placement does not change B's host-facing
network. For a same-host Docker A acceptance fixture, attach A to
`termflow-v020-local_default` (or use the exact project-specific `_default`
name) and point it at `http://control-plane:8000`; for a different host, point
A at B's canonical HTTPS/WSS origin. Do not create that bridge with
`--internal`.

Until the preflight succeeds, do not interpret a base-only health response, the
deterministic fake-provider tests, or a disposable proxy test as evidence that
this stable project can call DeepSeek or execute a Docker A command.

The durable resources are exactly:

- project `termflow-v020-local`;
- volume `termflow-v020-local-data`;
- volume `termflow-v020-local-totp-key`;
- volume `termflow-v020-local-opencode-data`.

Do not set legacy `TERMFLOW_DATA_VOLUME`, `TERMFLOW_TOTP_KEY_VOLUME`, or
`OPENCODE_DATA_VOLUME`; the stable Compose file intentionally ignores them.
Never run `down --volumes`, `docker volume rm`, or a prune command against this
deployment.

## Restart and persistence evidence

Before a planned restart, record only container IDs, volume IDs, service health,
and image digests. Do not inspect or log environment values. Restart only the
exact B and OpenCode services, then confirm the volume IDs are unchanged, B is
healthy, OpenCode health and `/mcp` return ready/connected, and the existing
Profile, Binding, conversations, and runtime epoch are preserved. A normal B
restart must not rotate the epoch.

Static tests, Compose rendering, and `/global/health` alone are not live-model
acceptance. The external provider, MCP tool, approval, Docker A command, and
terminal output gates remain separate evidence.

The 2026-09-09 disposable persistence check preserved the three volume IDs,
returned HTTP 200 for `/healthz` and Web C from the host, restored the existing
binding/runtime to `ready` at epoch 1 with Docker A online, and retained the
single final Agent response `1`. The live container inspector also passed.
