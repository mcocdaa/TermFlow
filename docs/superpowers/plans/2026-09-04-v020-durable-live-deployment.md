# v0.2.0 Durable Live Agent Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a persistent B + Web C + OpenCode deployment whose only provider egress is a digest-pinned allowlist proxy.

**Architecture:** Base Compose has a normal host-facing default bridge for B/Web C/A and no provider egress; OpenCode reaches B only through the internal agent network. `compose.agent-live.yaml` adds an internal provider network, a proxy-only uplink, and DeepSeek configuration. Static contracts and a label-based runtime inspector prove mounts, networks, identities, limits, and secret boundaries without depending on the original shell environment.

**Tech Stack:** Docker Compose, pinned OCI images, OpenCode 1.18.18, CONNECT proxy, POSIX shell, pytest, Docker inspect.

---

**Dependencies:** Runtime/product API plans must be reviewed. Cleanup helper token naming must be frozen. Existing temporary projects and volumes are out of scope and must remain untouched.

**Execution constraint:** Do not print secret values, run `down -v`, or remove any container/volume. Preserve dirty Compose/security files. Do not commit/push/reset/clean.

### Task 1: Freeze Base and Live Compose Contracts

**Files:**

- Create: `tests/deploy/test_agent_live_compose_contract.py`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `deploy/compose.yaml`
- Create: `deploy/compose.agent-live.yaml`
- Modify: `.env.example`

- [ ] **Step 1: Write Compose RED tests**

Create tests with the exact names
`test_base_opencode_config_bind_never_creates_missing_host_path`,
`test_live_opencode_has_only_two_internal_networks`,
`test_only_proxy_joins_provider_uplink`,
`test_proxy_has_no_host_ports_and_no_agent_internal_membership`,
`test_named_volumes_have_stable_explicit_names`, and
`test_runtime_and_proxy_images_are_literal_digest_pins`. Parse rendered Compose
YAML and assert exact service/network/volume/image values rather than substring
matches.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_compose_contract.py \
  tests/deploy/test_agent_live_compose_contract.py
```

- [ ] **Step 3: Implement the topology**

Base Compose uses top-level `name: termflow-v020-local`, explicit B/TOTP/OpenCode
volume names, and long bind syntax:

```yaml
- type: bind
  source: ./opencode-config.yaml
  target: /etc/termflow/opencode-config.yaml
  read_only: true
  bind:
    create_host_path: false
```

The live override joins OpenCode to `agent_internal` and internal
`provider_egress`; proxy joins only `provider_egress` and non-internal
`provider_uplink`. Only proxy receives an uplink. Add `HTTPS_PROXY` and the
exact `NO_PROXY=control-plane,opencode-agent,localhost,127.0.0.1` to OpenCode.
No service publishes an Agent/OpenCode/proxy host port.

- [ ] **Step 4: Verify GREEN and rendering**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_compose_contract.py \
  tests/deploy/test_agent_live_compose_contract.py
TERMFLOW_ADMIN_TOKEN=compose-contract-only \
OPENCODE_SERVER_USERNAME=termflow \
OPENCODE_SERVER_PASSWORD=compose-contract-only \
DEEPSEEK_API_KEY=compose-contract-only \
docker compose -p termflow-v020-local \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml config --quiet
git diff --check
```

### Task 2: Add a Digest-Pinned Allowlist Proxy

**Files:**

- Create: `deploy/provider-egress/squid.conf`
- Modify: `deploy/compose.agent-live.yaml`
- Modify: `tests/deploy/test_agent_live_compose_contract.py`

- [ ] **Step 1: Extend RED contracts**

Assert the configuration allows only `CONNECT api.deepseek.com:443`, rejects
raw IP destinations and all other ports/domains, disables access/cache logs
containing URLs, and has an explicit deny-all tail.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_live_compose_contract.py
```

- [ ] **Step 3: Resolve and pin the real image digest**

Pull the selected fixed proxy tag, inspect `.RepoDigests`, and hard-code the
observed digest in Compose. Do not use a tag-only or environment-overridable
image reference. Verify that the chosen image supports a non-root numeric user,
read-only root filesystem, tmpfs runtime paths, dropped capabilities,
no-new-privileges, and the configured unprivileged listen port before retaining
it. Record the tag-to-digest resolution in a test fixture comment without any
credential.

- [ ] **Step 4: Implement proxy policy and hardening**

The proxy service must declare numeric non-root `user`, `read_only: true`,
`cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, tmpfs for required
runtime state, healthcheck, CPU/memory/PID limits, no host port, and exactly two
networks. The configuration ends in deny-all.

- [ ] **Step 5: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_live_compose_contract.py
docker compose -p termflow-v020-local \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml config --quiet
git diff --check
```

### Task 3: Make OpenCode Configuration Provider-Ready

**Files:**

- Modify: `deploy/opencode-config.yaml`
- Modify: `deploy/compose.agent-live.yaml`
- Modify: `.env.example`
- Modify: `tests/deploy/test_agent_live_compose_contract.py`
- Modify: `tests/e2e/test_agent_opencode_container.py`

- [ ] **Step 1: Write provider/MCP RED tests**

Create tests named `test_opencode_config_selects_deepseek_without_literal_secret`,
`test_live_environment_uses_native_secret_reference_not_rendered_value`, and
`test_container_reports_termflow_mcp_connected`. Parse configuration structurally,
scan rendered output for only environment variable references, and query the
authenticated runtime MCP endpoint for the exact connected state.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_live_compose_contract.py \
  tests/e2e/test_agent_opencode_container.py
```

- [ ] **Step 3: Configure exact provider/model references**

Use OpenCode's pinned configuration schema and native environment reference for
the DeepSeek key. Configure provider ID `deepseek`, the user-approved model ID,
and base URL `https://api.deepseek.com`. Keep TermFlow MCP Authorization as a
native environment reference. Never render either secret into a generated file.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_live_compose_contract.py \
  tests/e2e/test_agent_opencode_container.py
git diff --check
```

### Task 4: Add a Read-Only Local Preflight

**Files:**

- Create: `scripts/deploy/agent-local-preflight.sh`
- Create: `tests/deploy/test_agent_local_preflight.py`

- [ ] **Step 1: Write fake-command RED tests**

Create tests named `test_preflight_rejects_env_mode_other_than_0600_without_printing_values`,
`test_preflight_rejects_config_directory_or_missing_regular_file`,
`test_preflight_lists_only_missing_variable_names`, and
`test_preflight_never_calls_up_down_rm_or_volume_remove`. Use a temporary fake
Docker executable that logs argv, secret fixture values, files at modes 0600 and
0644, a directory in place of config, and assert stdout/stderr and argv logs.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_local_preflight.py
```

- [ ] **Step 3: Implement the preflight CLI**

The script accepts `--env-file <absolute-path>` and checks: regular file mode
0600, required variable names, regular OpenCode/proxy config files, explicit
volume names, Docker availability, and both Compose renders. It prints variable
names only and performs no `up`, `down`, restart, delete, or secret echo.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/deploy/test_agent_local_preflight.py
git diff --check
```

### Task 5: Rewrite Runtime Security Inspection by Labels

**Files:**

- Modify: `scripts/security/verify-agent-containers.sh`
- Create: `tests/security/test_agent_container_security_script.py`
- Modify: `tests/security/test_agent_container_security_contract.py`

- [ ] **Step 1: Write fake-Docker RED tests**

Create tests named `test_script_discovers_exact_service_by_compose_labels_without_compose`,
`test_duplicate_or_missing_service_fails_closed`,
`test_live_topology_rejects_opencode_on_uplink`,
`test_live_topology_rejects_proxy_on_agent_internal`, and
`test_output_never_contains_fixture_secrets_or_full_inspect_json`. The fake
Docker executable returns fixed label/network/security JSON and records every
subcommand; assert no `compose` subcommand and no fixture secret in output.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/security
```

- [ ] **Step 3: Implement exact CLI behavior**

```text
verify-agent-containers.sh --mode offline|live <exact-compose-project>
```

Discover each service with `docker ps -aq` and exact
`com.docker.compose.project`/`service` label filters; require one container. Do
not call `docker compose`. Inspect only selected fields. Offline expects one
internal OpenCode network. Live expects OpenCode on two internal networks,
proxy on provider-egress plus uplink, uplink membership containing only proxy,
and proxy absent from agent-internal. Retain UID/rootfs/capability/NNP/tmpfs/
limits/digest/no-host-port/secret-name checks.

- [ ] **Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/security
git diff --check
```

### Task 6: Deployment Slice Verification

**Files:** Verify only; runtime mutation occurs only in the final acceptance plan.

- [ ] **Step 1: Run static/render gates**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/deploy tests/security
scripts/deploy/agent-local-preflight.sh \
  --env-file /home/mcocdaa/AI_CODE/TermFlow/.env
docker compose -p termflow-v020-local \
  --env-file /home/mcocdaa/AI_CODE/TermFlow/.env \
  -f deploy/compose.yaml -f deploy/compose.agent-live.yaml config --quiet
git diff --check
```

- [ ] **Step 2: Record exact evidence**

Record exit codes and rendered topology summary without values. If `.env` is
not 0600, stop before deployment and report that exact preflight blocker; do not
silently weaken the check.
