# TermFlow V1 End-to-End Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the complete TermFlow V1 behavior with real processes and tmux, package the single-worker Control Plane for deployment, and deliver exact operator/API/security documentation.

**Architecture:** Treat the completed protocol, Control Plane, and Node as black-box processes in end-to-end tests. The deployment runs only B in Docker with persistent SQLite; A remains a host-installed CLI because it must own the user's real tmux session.

**Tech Stack:** Python 3.12, uv, pytest, pexpect, tmux 3.2+, FastAPI/Uvicorn, Docker, Docker Compose, curl, websockets.

---

## Preconditions

Complete both earlier plans and run:

```bash
uv run --all-packages pytest packages/protocol/tests apps/control-plane/tests apps/node/tests -q
```

Expected: all tests pass before adding delivery tests.

### Task 1: Add a real enrollment-to-Pane-input end-to-end harness

**Files:**
- Modify: `apps/node/pyproject.toml`
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_remote_pane_control.py`
- Test: `tests/e2e/test_remote_pane_control.py`

- [ ] **Step 1: Add pexpect as a Node test dependency**

Add `pexpect>=4.9,<5` to `apps/node/pyproject.toml` under `dependency-groups.dev`, then run
`uv lock`. It is used only to drive the real local tmux client in Unix CI.

- [ ] **Step 2: Write the failing end-to-end test**

```python
@pytest.mark.e2e
@pytest.mark.tmux
def test_enroll_create_detach_and_remote_control(termflow_system) -> None:
    enrollment = termflow_system.create_enrollment()
    termflow_system.login(enrollment)
    instance = termflow_system.new_and_detach("e2e-main")

    assert termflow_system.wait_until_online(instance.id)
    pane = termflow_system.first_pane(instance.id)
    event_cursor = termflow_system.subscribe(instance.id)

    result = termflow_system.send_text(
        instance.id,
        pane.pane_id,
        "printf 'E2E_REMOTE_OK\\n'",
        submit=True,
    )
    assert result["ok"] is True
    assert event_cursor.wait_for_bytes(b"E2E_REMOTE_OK", timeout=5)
    assert termflow_system.local_tmux_is_alive(instance)
```

- [ ] **Step 3: Run the test before creating the harness**

Run: `uv run --all-packages pytest tests/e2e/test_remote_pane_control.py -q`

Expected: collection FAIL because `termflow_system` does not exist.

- [ ] **Step 4: Implement isolated process fixtures**

The harness must create one temporary root containing config, state, runtime, SQLite, logs, and an
explicit port. Set `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, and B settings only for
child processes. Start B using:

```python
[
    "uv", "run", "--package", "termflow-control-plane",
    "termflow-control", "serve", "--host", "127.0.0.1", "--port", str(port),
]
```

Wait on `/healthz` with a five-second deadline. Track every child pid and explicit tmux socket;
cleanup sends graceful termination, then kills only tracked children after a bounded timeout.

- [ ] **Step 5: Implement CLI and tmux driving**

Use the B HTTP API to create enrollment, invoke `termflow login`, and spawn `termflow new --name
e2e-main` under pexpect. Wait for the tmux status line or query the new metadata record, then send
the configured tmux detach sequence. Read `termflow list --json` to obtain the exact Instance UUID
and socket path. Never inspect or send commands to the default tmux socket.

- [ ] **Step 6: Implement API and event helpers**

Use Admin bearer headers and a new UUID Idempotency-Key for each input. `subscribe` opens
`/api/v1/events?instance_id=<uuid>`, decodes `pane.output.data_base64`, and waits by monotonic
deadline. A replay subscription adds `pane_id`, `stream_id`, and `after_seq` together as query
parameters. The test must issue the input only after the initial topology identifies an existing
Pane.

- [ ] **Step 7: Run the real end-to-end test**

Run: `uv run --all-packages pytest tests/e2e/test_remote_pane_control.py -q -m e2e`

Expected: `1 passed`; process cleanup leaves no tmux socket or child owned by the test.

- [ ] **Step 8: Commit the core end-to-end proof**

```bash
git add tests/e2e apps/node/pyproject.toml uv.lock
git commit -m "test(e2e): prove remote Pane control"
```

### Task 2: Verify disconnect, isolation, replay, and privacy end to end

**Files:**
- Create: `tests/e2e/test_reconnect_and_isolation.py`
- Create: `tests/e2e/test_output_replay.py`
- Create: `tests/e2e/test_no_content_persistence.py`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Write the B-restart test**

```python
@pytest.mark.e2e
@pytest.mark.tmux
def test_b_restart_does_not_stop_instance(termflow_system) -> None:
    instance = termflow_system.enroll_new_and_detach("restart-case")
    termflow_system.stop_control_plane()
    assert termflow_system.local_tmux_is_alive(instance)
    termflow_system.local_send(instance, "printf 'LOCAL_DURING_B_DOWN\\n'", submit=True)
    termflow_system.start_control_plane()
    assert termflow_system.wait_until_online(instance.id, timeout=10)
    assert termflow_system.topology(instance.id).windows
```

- [ ] **Step 2: Write the two-Instance isolation test**

Create two `termflow new` Instances on the same Installation. Assert distinct UUIDs, tmux sockets,
Bridge pids, and B WSS registrations. Inject `ONLY_A` and `ONLY_B` into their respective Panes and
assert the filtered event streams never cross. Kill A and assert B's local tmux and WSS stay live.

- [ ] **Step 3: Write replay and gap tests**

Subscribe, record a `stream_id + seq`, disconnect the subscriber, generate output smaller than the
Bridge ring limit, reconnect and request replay, then assert exact missing bytes arrive once. Next
generate more than the test-configured 64-byte ring limit and assert `stream.gap` followed by a
capture snapshot and a new stream ID.

- [ ] **Step 4: Write the no-content-persistence test**

Use sentinel `SECRET_E2E_TERMINAL_38e6`. Send it through remote input, confirm it appears in the
live Pane event, shut down cleanly, checkpoint SQLite, and inspect only B database files, B logs,
Node metadata/logs, and config files. Assert the sentinel is absent. Assert raw enrollment,
Installation, and Instance credentials are absent from those same files and logs.

- [ ] **Step 5: Verify offline rejection has no delayed effect**

Stop one Bridge while preserving its tmux server, call Pane input, assert HTTP 409
`instance_offline`, restart Bridge, and assert the rejected sentinel never appears in its Pane.

- [ ] **Step 6: Run the resilience suite**

Run: `uv run --all-packages pytest tests/e2e/test_reconnect_and_isolation.py tests/e2e/test_output_replay.py tests/e2e/test_no_content_persistence.py -q -m e2e`

Expected: all tests pass with no orphan child process or socket.

- [ ] **Step 7: Commit resilience evidence**

```bash
git add tests/e2e
git commit -m "test(e2e): verify reconnect isolation and privacy"
```

### Task 3: Containerize the single-worker Control Plane

**Files:**
- Create: `.dockerignore`
- Create: `deploy/Dockerfile.control-plane`
- Create: `deploy/compose.yaml`
- Create: `deploy/env.example`
- Test: `tests/deploy/test_compose_contract.py`

- [ ] **Step 1: Write the failing Compose contract test**

```python
from pathlib import Path

import yaml


def test_compose_is_single_worker_and_persists_only_metadata() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == ["termflow-data:/app/data"]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
```

- [ ] **Step 2: Run the contract test and confirm failure**

Run: `uv run --all-packages pytest tests/deploy/test_compose_contract.py -q`

Expected: FAIL because deployment files do not exist.

- [ ] **Step 3: Create a reproducible Control Plane image**

Use `python:3.12-slim`, copy the uv binary from the official `ghcr.io/astral-sh/uv` image, install
only the protocol and Control Plane locked packages with `uv sync --frozen --no-dev --package
termflow-control-plane`, create non-root user `termflow`, create `/app/data`, and run:

```dockerfile
CMD ["uv", "run", "--frozen", "--no-dev", "--package", "termflow-control-plane", \
     "termflow-control", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

Do not copy `.git`, local databases, `.env`, tests, caches, or user state into the image.

- [ ] **Step 4: Create Compose and environment example**

Define exactly one `control-plane` service, one named `termflow-data` volume, port `127.0.0.1:8000:8000`
by default, restart policy `unless-stopped`, and a `/healthz` healthcheck. Require
`TERMFLOW_ADMIN_TOKEN`; configure database URL `sqlite+aiosqlite:////app/data/termflow.db` and
`TERMFLOW_ALLOW_INSECURE_LOOPBACK=false`. The example env contains a generation command, never a
working secret:

```text
# Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'
TERMFLOW_ADMIN_TOKEN=replace-with-generated-secret
```

- [ ] **Step 5: Validate and test the container**

```bash
docker compose -f deploy/compose.yaml config --quiet
docker build -f deploy/Dockerfile.control-plane -t termflow-control-plane:v1-test .
docker run --rm -d --name termflow-control-plane-v1-test \
  -e TERMFLOW_ADMIN_TOKEN=test-admin-token-which-is-long-enough \
  -e TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  -p 127.0.0.1:18000:8000 termflow-control-plane:v1-test
curl --fail --silent --show-error http://127.0.0.1:18000/healthz
docker stop termflow-control-plane-v1-test
```

Expected: Compose config exits 0, build succeeds, health returns `{"status":"ok"}`, and the
explicit test container stops cleanly.

- [ ] **Step 6: Run the Compose contract test**

Run: `uv run --all-packages pytest tests/deploy/test_compose_contract.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit deployment assets**

```bash
git add .dockerignore deploy tests/deploy
git commit -m "build: containerize TermFlow Control Plane"
```

### Task 4: Write operator, architecture, protocol, security, and API documentation

**Files:**
- Create: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/protocol.md`
- Create: `docs/security.md`
- Create: `docs/api-examples.md`
- Create: `docs/troubleshooting.md`
- Modify: `apps/clients/README.md`
- Test: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Write the failing documentation contract**

```python
from pathlib import Path


def test_docs_state_v1_boundaries_and_never_show_special_key_api() -> None:
    all_docs = "\n".join(path.read_text() for path in Path("docs").glob("*.md"))
    assert "termflow new" in all_docs
    assert "/panes/{pane_id}/input" in all_docs
    assert "不持久化" in all_docs
    assert "/keys" not in all_docs
    assert "Kafka 是 V1 必需" not in all_docs
```

- [ ] **Step 2: Run the docs test and confirm failure**

Run: `uv run --all-packages pytest tests/docs/test_documentation_contract.py -q`

Expected: FAIL because the user documentation does not exist.

- [ ] **Step 3: Write README quick start**

Include prerequisites Python 3.12, uv, tmux 3.2+, B startup, enrollment creation, `termflow login`,
`termflow new`, tmux detach/attach, Instance listing, exact V1 scope, and links to all detailed
docs. Clearly state A is one `termflow new` Instance, not one physical computer.

- [ ] **Step 4: Write architecture and protocol docs**

Architecture must show local tmux client/server, per-Instance Bridge/WSS, B, and future C. Protocol
must document every V1 HTTP/WSS message, envelope field, Pane ID, Base64 output, seq/replay/gap,
heartbeats, Idempotency-Key, error mapping, and the explicit absence of special keys and remote Pane
lifecycle operations.

- [ ] **Step 5: Write security and troubleshooting docs**

Security must explain remote text is equivalent to trusted terminal control, private socket modes,
credential scopes, TLS, no token in URL, metadata-only audit, and no terminal persistence.
Troubleshooting must provide non-destructive checks for tmux version, `termflow doctor`, Bridge
offline, B restart, token revocation, and safe cleanup using exact Instance UUIDs.

- [ ] **Step 6: Write executable curl and Python examples**

Show Admin token through an environment variable without echoing it, create enrollment, list
Instances, fetch topology, send plain input with a generated Idempotency-Key, and subscribe to
events using Python websockets. The input example must be exactly ordinary text plus `submit`; no
Ctrl+C or generic key API.

- [ ] **Step 7: Update the C boundary README**

State that V1 contains no client code. Record that a future C authenticates only with B, renders
Base64 terminal bytes, selects existing Panes, and uses the same protocol. STT and a B-side Agent
are explicitly outside V1 and cannot bypass the control API.

- [ ] **Step 8: Run docs tests and manually execute examples against a smoke B**

Run: `uv run --all-packages pytest tests/docs/test_documentation_contract.py -q`.

Then start the loopback smoke B, execute each curl example with test credentials, and run the
Python event subscriber. Expected: documented status codes and response shapes match actual
OpenAPI/WSS behavior; no example prints secret values.

- [ ] **Step 9: Commit documentation**

```bash
git add README.md docs apps/clients tests/docs
git commit -m "docs: document TermFlow V1 operations"
```

### Task 5: Add CI and run final release verification

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/verify.sh`
- Create: `tests/test_repository_contract.py`

- [ ] **Step 1: Write the failing repository contract test**

```python
from pathlib import Path


def test_v1_repository_has_required_delivery_artifacts() -> None:
    required = [
        "uv.lock",
        "deploy/Dockerfile.control-plane",
        "deploy/compose.yaml",
        "docs/architecture.md",
        "docs/protocol.md",
        "docs/security.md",
        "docs/api-examples.md",
        "docs/troubleshooting.md",
        ".github/workflows/ci.yml",
    ]
    assert [path for path in required if not Path(path).is_file()] == []
```

- [ ] **Step 2: Run the repository contract and confirm failure**

Run: `uv run --all-packages pytest tests/test_repository_contract.py -q`

Expected: FAIL until CI and verification script exist.

- [ ] **Step 3: Create the verification script**

`scripts/verify.sh` must use `set -euo pipefail`, resolve repository root without assuming the
current directory, and run exactly:

```bash
uv sync --frozen --all-packages
uv run --all-packages pytest -q
uv run --all-packages ruff check .
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
docker compose -f deploy/compose.yaml config --quiet
```

It must not delete caches, databases, tmux servers, containers, or user files.

- [ ] **Step 4: Create Linux CI**

The workflow checks out code, installs Python 3.12, uv, and tmux, runs `uv sync --frozen
--all-packages`, executes unit/integration/e2e tests, Ruff, mypy, Compose config, and builds the B
image. Set XDG directories to job-temporary paths. Upload only pytest/JUnit metadata on failure;
never upload databases or Bridge logs that could contain environment details.

- [ ] **Step 5: Run repository contract and full verification**

```bash
uv run --all-packages pytest tests/test_repository_contract.py -q
bash scripts/verify.sh
docker build -f deploy/Dockerfile.control-plane -t termflow-control-plane:v1-verification .
```

Expected: every command exits 0; no required test is skipped.

- [ ] **Step 6: Perform the approved acceptance walkthrough**

Using only temporary test configuration:

1. start B;
2. generate one enrollment;
3. login one Installation;
4. create two independent Instances;
5. detach both local clients;
6. split a Pane locally in one Instance;
7. list both Instances and fetch topology through B;
8. remotely input ordinary text to one existing Pane;
9. observe output through the event WSS;
10. stop B and prove both tmux servers remain alive;
11. restart B and prove both Bridges reconnect;
12. inspect B storage/logs for absence of the test terminal sentinel;
13. kill only the two explicit test Instances.

Record commands and pass/fail evidence in the implementation handoff, not in a terminal-content
recording.

- [ ] **Step 7: Commit final delivery automation**

```bash
git add .github scripts tests pyproject.toml uv.lock
git commit -m "ci: verify TermFlow V1 delivery"
```

- [ ] **Step 8: Confirm the branch is ready for completion workflow**

Run `git status --short` and `git log --oneline -12`.

Expected: clean worktree and a sequence of focused commits matching the three plans. Then invoke
`superpowers:verification-before-completion`; after fresh evidence passes, invoke
`superpowers:finishing-a-development-branch` to choose merge, PR, or cleanup.
