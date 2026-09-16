# Agent Broker durable local deployment

This runbook owns the stable v0.2.0 B + Web C + OpenCode deployment. It does
not authorize cleanup of an older Compose project, container, network, or
volume; preserve those resources until their exact IDs are separately reviewed.

## Current gate status (2026-09-10)

The merged v0.2.0 tree passed the repository gate (`scripts/verify.sh`: 1136
pytest cases, ruff, mypy, Tauri tests, Compose contract, and Control Plane
image build/verification) and `scripts/verify-node-image.sh` for the locally
built `termflow-node:v0.2.0`. A fresh base/offline deployment of the fixed
project `termflow-v020-local` was created with new named volumes and new
secrets: B published Web C and the A/C API on `127.0.0.1:8765` (`/healthz` HTTP
200, `/api/v1/agent/capabilities` `enabled/ready`), the pinned OpenCode
container is healthy, and the offline container inspector
(`scripts/security/verify-agent-containers.sh --mode offline
termflow-v020-local`) passed.

A same-host Docker A built from the same checkout joined
`termflow-v020-local_default`, enrolled with a single-use code, appeared
`online`, and completed the B topology → pane input → pane output path (see
"Docker A same-host fixture"). The 13 live provider-policy fields and
`DEEPSEEK_API_KEY` are intentionally absent from the base profile, so provider
readiness remains fail-closed: no real model, MCP tool, approval, or
Agent-driven Docker A command is verified here. The same project has since
been upgraded to the live profile with an operator-supplied DeepSeek credential
and disclosure fields; the real model path (one approval-gated `send_text`,
Docker A `echo 1`, provider readiness `verified`) is recorded in
[the live-model runbook](agent-broker-live-model.md).

The canonical browser acceptance suite (`scripts/run-web-e2e.sh`, Playwright
desktop + Pixel 7 portrait/landscape) passed 23 tests with 4 expected skips
after the v0.2.0 browser regressions were repaired: the capability-gated Agent
navigation count, asynchronous 202 Term deletion with its cleanup banner, the
terminal word-selection prompt race, the `管理员令牌` dialog labels, the
landscape side navigation, and the read-only Agent directory with its
disabled-binding fence. The pass also exposed and fixed a mobile layout defect:
the terminal interaction grid lacked an explicit column, so the keybar
inherited the terminal's intrinsic width. The deployed browser smoke
(`deployed-smoke.spec.ts`, three viewports) and the 57-check deployed API smoke
passed against the rebuilt stack.

The earlier disposable `termflow-v020-live-0909` record and the removed
`termflow-v020-local-0903` resources are historical; do not treat either as the
stable deployment.

## Prepare without changing runtime state

From the repository root, create the deployment file once, generate distinct
random values for every required secret placeholder, and restrict its
permissions:

```bash
cp .env.example .env
chmod 0600 .env
```

The base/offline profile needs `TERMFLOW_ADMIN_TOKEN`,
`OPENCODE_AGENT_MCP_TOKEN`, `OPENCODE_SERVER_USERNAME`, and
`OPENCODE_SERVER_PASSWORD`; render it before starting:

```bash
docker compose -p termflow-v020-local --env-file .env \
  -f deploy/compose.yaml config --quiet
```

The live profile additionally requires `DEEPSEEK_API_KEY` and the complete
`TERMFLOW_AGENT_PROVIDER_DEEPSEEK_*` policy fields, so run the non-mutating
gate before any live lifecycle command:

```bash
scripts/deploy/agent-local-preflight.sh --env-file "$PWD/.env"
```

The policy fields deliberately remain incomplete in `.env.example`. Supply
`no_training=true` only with account/contract evidence for this exact provider
configuration. Without it, preflight failure is the correct fail-closed result.
The preflight performs reads and Compose rendering only and must never print
credential values.

## Start the base offline profile

The base profile boots B + Web C + the pinned OpenCode container with the
provider catalog fail-closed. It is the supported deployment for exercising the
A/C terminal path and the Agent terminal sidecar without a model provider; it
is not provider, MCP, or live-model evidence.

```bash
docker compose -p termflow-v020-local --env-file .env \
  -f deploy/compose.yaml up -d --build
scripts/security/verify-agent-containers.sh --mode offline termflow-v020-local
curl -fsS http://127.0.0.1:8765/healthz
```

## Docker A same-host fixture

When no release image matches the checkout, build A from the same checkout as
B:

```bash
scripts/build-node-image.sh termflow-node:v0.2.0
```

Create identity and work directories outside the repository, then mint one
single-use enrollment code (default TTL 60 seconds) with the repository-root
administrator token and start A on the project's default bridge:

```bash
mkdir -p "$HOME/termflow-node-a/identity" "$HOME/termflow-node-a/work"
code="$(curl -fsS -X POST \
  -H "Authorization: Bearer $(grep '^TERMFLOW_ADMIN_TOKEN=' .env | cut -d= -f2)" \
  http://127.0.0.1:8765/api/v1/enrollment-tokens \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')"

docker run -d --name termflow-node-a \
  --restart unless-stopped \
  --network termflow-v020-local_default \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges:true --read-only --tmpfs /tmp \
  --volume "$HOME/termflow-node-a/identity:/home/termflow" \
  --volume "$HOME/termflow-node-a/work:/work" \
  --env TERMFLOW_SERVER=http://control-plane:8000 \
  --env TERMFLOW_CODE="${code}" \
  --env TERMFLOW_ALLOW_INSECURE_HTTP=true \
  --env TERMFLOW_NEW=demo \
  termflow-node:v0.2.0
```

Verify from B and from inside A. `docker exec` must pass `--user termflow`:
the Bridge and every private tmux server run as container UID 1000, so a root
exec cannot see the instance socket, and the instance uses a private socket
name rather than the tmux default.

```bash
docker logs termflow-node-a   # "enrolled at control-plane", then "serve: instance ... running"
docker exec --user termflow termflow-node-a termflow doctor
docker exec --user termflow termflow-node-a termflow status demo --json
curl -fsS -H "Authorization: Bearer $(grep '^TERMFLOW_ADMIN_TOKEN=' .env | cut -d= -f2)" \
  http://127.0.0.1:8765/api/v1/instances
```

The 2026-09-10 check observed the instance `online`, opened the
`/api/v1/events` stream, posted `printf <marker>` to pane `%0` through
`/api/v1/instances/{id}/panes/%0/input` (HTTP 200), and received the marker in
`pane.output` on the same stream. An expired code fails the login inside the
container; mint a new one and recreate the container with the same identity
directory.

Agent-driven execution through this Docker A still requires the live profile
and its provider-policy evidence; use
[the live-model runbook](agent-broker-live-model.md) for that gate.

## Start the live profile

The supported Agent live slice gives the runtime a dedicated direct provider
uplink (no filtering proxy, see docs/security.md「运行时出网边界」):

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
(B to OpenCode) is an internal network; `provider_uplink` is a separate normal
bridge used for direct provider TLS. Docker A
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
