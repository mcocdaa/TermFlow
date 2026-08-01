# TermFlow V1 Node, tmux, and Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local `termflow` application so each `termflow new` owns an isolated tmux server, a durable-on-detach Bridge process, and an independently authenticated WSS connection to the completed Control Plane.

**Architecture:** The CLI manages private local state and delegates terminal multiplexing to the installed tmux binary. A per-Instance Bridge attaches through tmux control mode, keeps output only in bounded memory, registers/reconnects independently, and executes only validated plain-text Pane input with per-Pane ordering and idempotency.

**Tech Stack:** Python 3.12, uv, asyncio, Typer, platformdirs, HTTPX, websockets, Pydantic protocol package, tmux 3.2+, pytest and real-tmux integration tests.

---

## Preconditions and file map

Complete `2026-08-01-termflow-v1-control-plane.md` first. Its full test suite must pass.

Create these units:

- `config/{models,store}.py`: Installation configuration and secure local persistence.
- `instances/{models,store,manager}.py`: one local record and process boundary per Instance.
- `tmux/{runner,control_parser,control_client,topology}.py`: subprocess-safe tmux integration.
- `bridge/{buffer,idempotency,transport,runtime}.py`: in-memory output, WSS, registration, and input.
- `cli.py`: the only public user command; Bridge execution is a hidden subcommand.

### Task 1: Implement secure local configuration and `termflow login`

**Files:**
- Create: `apps/node/src/termflow_node/config/__init__.py`
- Create: `apps/node/src/termflow_node/config/models.py`
- Create: `apps/node/src/termflow_node/config/store.py`
- Create: `apps/node/src/termflow_node/control_plane_client.py`
- Create: `apps/node/src/termflow_node/cli.py`
- Create: `apps/node/src/termflow_node/__main__.py`
- Modify: `apps/node/pyproject.toml`
- Test: `apps/node/tests/test_config_store.py`
- Test: `apps/node/tests/test_login.py`

- [ ] **Step 1: Write failing secure-store tests**

```python
import stat

from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore


def test_config_is_atomic_private_and_round_trips(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    expected = InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id="0b1f5b0f-51ee-4df6-baae-c25f0763917e",
        installation_token="secret-token",
    )
    store.save(expected)
    assert store.load() == expected
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run the Node tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_config_store.py apps/node/tests/test_login.py -q`

Expected: collection FAIL because config and CLI modules do not exist.

- [ ] **Step 3: Implement local models and atomic persistence**

Define:

```python
class InstallationConfig(BaseModel):
    server_url: AnyHttpUrl
    installation_id: UUID
    installation_token: SecretStr
```

`ConfigStore.default()` uses `platformdirs.user_config_path("termflow") / "config.json"`.
`save` creates the parent with mode `0700`, writes JSON to a same-directory temporary file using
`os.open(..., 0o600)`, `fsync`s it, and atomically calls `os.replace`. `load` rejects files whose
owner is not the current uid or whose group/other permission bits are nonzero. Secret values must
not appear in `repr`. Because Pydantic masks `SecretStr` during ordinary JSON serialization,
`ConfigStore.save` must explicitly write `installation_token.get_secret_value()` into the private
disk DTO; loading reconstructs `SecretStr`. Tests must assert the on-disk value is the real token,
not the string `**********`, while stdout, repr, and logs remain redacted.

- [ ] **Step 4: Implement the enrollment HTTP client**

Create an async `ControlPlaneClient.enroll(server_url, enrollment_token)` that posts to
`/api/v1/installations/enroll`, uses a 10-second timeout, validates
`InstallationEnrollResponse`, calls `response.raise_for_status()`, and never logs request bodies.
Permit `http://127.0.0.1` and `http://localhost` only; all non-loopback URLs must be HTTPS.

- [ ] **Step 5: Implement `termflow login`**

Typer command signature:

```python
def login(
    server: str = typer.Option(..., "--server"),
    enrollment_token: str = typer.Option(..., "--enrollment-token", prompt=True, hide_input=True),
) -> None:
    ...
```

Call the async client, persist the returned Installation configuration, print only the
Installation ID and server hostname, and overwrite no existing login unless `--force` is supplied.
Add:

```toml
[project.scripts]
termflow = "termflow_node.cli:app"
```

- [ ] **Step 6: Test successful login, TLS policy, and secret redaction**

Use `httpx.MockTransport` to return a fixed Installation response. Assert the config is private,
stdout/stderr and captured logs omit both raw tokens, a public `http://` URL is rejected, and a
second login requires `--force`.

Run: `uv run --package termflow-node pytest apps/node/tests/test_config_store.py apps/node/tests/test_login.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit login and configuration**

```bash
git add apps/node uv.lock
git commit -m "feat(node): add secure TermFlow login"
```

### Task 2: Implement private Instance state and tmux lifecycle

**Files:**
- Create: `apps/node/src/termflow_node/instances/__init__.py`
- Create: `apps/node/src/termflow_node/instances/models.py`
- Create: `apps/node/src/termflow_node/instances/store.py`
- Create: `apps/node/src/termflow_node/tmux/__init__.py`
- Create: `apps/node/src/termflow_node/tmux/runner.py`
- Create: `apps/node/src/termflow_node/instances/manager.py`
- Test: `apps/node/tests/test_instance_store.py`
- Test: `apps/node/tests/test_tmux_runner.py`
- Test: `apps/node/tests/integration/test_tmux_lifecycle.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
import os

import pytest

from termflow_node.tmux.runner import TmuxRunner


@pytest.mark.tmux
def test_private_tmux_server_survives_client_detach(tmp_path) -> None:
    socket_path = tmp_path / "instance.sock"
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "termflow-test")
    try:
        assert runner.is_alive()
        assert runner.list_pane_ids() == ["%0"]
        assert socket_path.exists()
        assert os.stat(socket_path).st_uid == os.getuid()
    finally:
        runner.kill_server()
    assert not runner.is_alive()
```

- [ ] **Step 2: Run lifecycle tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_instance_store.py apps/node/tests/test_tmux_runner.py apps/node/tests/integration/test_tmux_lifecycle.py -q`

Expected: collection FAIL because Instance and tmux modules do not exist.

- [ ] **Step 3: Implement Instance metadata and state layout**

Define `LocalInstance` with UUID, name, session name fixed to `main`, socket path, created time,
Bridge pid, optional Instance token, and lifecycle enum `starting|running|stopped|broken`.
`InstanceStore.default()` uses `platformdirs.user_state_path("termflow") / "instances"`. Each
Instance gets a `0700` directory and `0600` `metadata.json`; list ignores malformed records and
reports their paths as diagnostics rather than deleting them. Model `instance_token` as
`SecretStr`; the store uses an explicit private disk DTO to persist its real value, while normal
model dumps, repr, CLI JSON, and logs exclude it.

- [ ] **Step 4: Implement tmux subprocess calls without a shell**

`TmuxRunner` receives an explicit absolute socket path and uses argument arrays only:

```python
["tmux", "-S", str(socket_path), "new-session", "-d", "-s", session_name, "-n", window_name]
["tmux", "-S", str(socket_path), "has-session", "-t", session_name]
["tmux", "-S", str(socket_path), "attach-session", "-t", session_name]
["tmux", "-S", str(socket_path), "kill-server"]
```

Before use, parse `tmux -V` and reject versions below 3.2. Check socket path byte length against
the platform Unix-domain limit before spawning. Return typed `TmuxCommandError` containing argv
and exit code but not Pane content.

- [ ] **Step 5: Implement atomic creation and cleanup**

`InstanceManager.create(name)` generates a UUID, creates state, starts tmux detached, starts the
Bridge implemented in Task 7, marks running, then returns the attach argv. If tmux creation fails, remove only
that newly created state directory. Never call a recursive delete on the state root. `kill` sends
Bridge SIGTERM if its recorded pid still belongs to the expected command, kills only the explicit
tmux socket, and marks stopped.

- [ ] **Step 6: Run fake and real tmux tests**

Run: `uv run --package termflow-node pytest apps/node/tests/test_instance_store.py apps/node/tests/test_tmux_runner.py apps/node/tests/integration/test_tmux_lifecycle.py -q`

Expected: tests pass; no default user tmux socket is created or accessed.

- [ ] **Step 7: Commit Instance and tmux lifecycle**

```bash
git add apps/node
git commit -m "feat(node): manage isolated tmux Instances"
```

### Task 3: Parse tmux control mode and build topology snapshots

**Files:**
- Create: `apps/node/src/termflow_node/tmux/control_parser.py`
- Create: `apps/node/src/termflow_node/tmux/topology.py`
- Create: `apps/node/src/termflow_node/tmux/control_client.py`
- Test: `apps/node/tests/test_control_parser.py`
- Test: `apps/node/tests/test_topology.py`
- Test: `apps/node/tests/integration/test_control_mode.py`

- [ ] **Step 1: Write failing control-mode parser tests**

```python
from termflow_node.tmux.control_parser import OutputNotification, parse_control_line


def test_output_notification_preserves_arbitrary_bytes() -> None:
    event = parse_control_line(b"%output %7 hi\\015\\012\\033[31m\\134x\n")
    assert event == OutputNotification("%7", b"hi\r\n\x1b[31m\\x")


def test_non_output_notification_remains_structured() -> None:
    event = parse_control_line(b"%window-add @3\n")
    assert event.name == "window-add"
    assert event.arguments == ("@3",)
```

- [ ] **Step 2: Run parser tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_control_parser.py apps/node/tests/test_topology.py -q`

Expected: collection FAIL because parser and topology modules do not exist.

- [ ] **Step 3: Implement byte-preserving parsing**

Read control mode stdout as bytes. Parse `%output <pane-id> <escaped-data>` without decoding the
payload. Replace only `\\ooo` octal escapes with their byte values; reject incomplete or non-octal
escapes as `MalformedControlNotification`. Parse `%begin/%end/%error`, topology notifications,
`%pause`, `%continue`, and `%exit` into dataclasses. Unknown `%name` values remain generic
notifications so a newer tmux does not crash the Bridge.

- [ ] **Step 4: Implement deterministic topology queries**

Use `tmux -S <socket> list-windows` and `list-panes -a` with explicit `-F` formats containing IDs,
indexes, dimensions, active/dead flags, and `q`-escaped names/titles. Parse with `shlex.split`,
group Pane snapshots under Window snapshots, require exactly the managed Session, and increment a
local revision only when the validated topology value changes.

- [ ] **Step 5: Implement the control mode subprocess**

Start:

```python
[
    "tmux", "-S", str(socket_path), "-C", "attach-session", "-t", session_name,
]
```

Set control flow with `refresh-client -f pause-after=5`. Provide async iteration of notifications,
a command writer serialized by an asyncio lock, clean EOF handling, and `capture_pane(pane_id)` via
`tmux capture-pane -p -e -S - -t <pane-id>` returning bytes.

- [ ] **Step 6: Verify against a real tmux server**

Create a temporary server, split a Pane locally using `tmux split-window`, write bytes with
`printf`, and assert control mode yields the correct Pane ID/output and topology changes from one
to two Panes. Kill only the temporary server in `finally`.

Run: `uv run --package termflow-node pytest apps/node/tests/test_control_parser.py apps/node/tests/test_topology.py apps/node/tests/integration/test_control_mode.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit tmux observation**

```bash
git add apps/node
git commit -m "feat(node): observe tmux control mode"
```

### Task 4: Add bounded in-memory output streams and replay

**Files:**
- Create: `apps/node/src/termflow_node/bridge/__init__.py`
- Create: `apps/node/src/termflow_node/bridge/buffer.py`
- Test: `apps/node/tests/test_output_buffer.py`

- [ ] **Step 1: Write failing ring-buffer tests**

```python
from termflow_node.bridge.buffer import PaneOutputBuffer, ReplayGap


def test_buffer_replays_by_stream_and_sequence() -> None:
    buffer = PaneOutputBuffer(max_bytes=8)
    first = buffer.append(b"abc")
    second = buffer.append(b"de")
    assert [chunk.data for chunk in buffer.replay(first.stream_id, first.seq)] == [b"de"]
    assert second.seq == first.seq + 1


def test_overwrite_reports_gap() -> None:
    buffer = PaneOutputBuffer(max_bytes=4)
    old = buffer.append(b"abc")
    buffer.append(b"def")
    assert isinstance(buffer.replay(old.stream_id, 0), ReplayGap)
```

- [ ] **Step 2: Run buffer tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_output_buffer.py -q`

Expected: collection FAIL because `PaneOutputBuffer` does not exist.

- [ ] **Step 3: Implement exact byte-bound eviction**

Each buffer creates a UUID stream, increments seq from 1, stores immutable chunks in a deque, and
tracks exact byte totals. Appending one chunk larger than the limit keeps only its final
`max_bytes` bytes and marks all prior positions unavailable. `replay(stream_id, after_seq)` returns
ordered chunks or `ReplayGap(reason="stream_changed|overwritten")`. No method writes to disk.

- [ ] **Step 4: Add a per-Pane buffer registry**

Implement `OutputBuffers(max_bytes_per_pane)` with lazy creation, append, replay, remove, and
`reset_stream`. Removing a Pane releases all bytes. Expose current total bytes for tests and
metrics, never output contents.

- [ ] **Step 5: Run buffer tests and static checks**

Run: `uv run --package termflow-node pytest apps/node/tests/test_output_buffer.py -q && uv run --package termflow-node ruff check apps/node/src/termflow_node/bridge/buffer.py && uv run --package termflow-node mypy apps/node/src/termflow_node/bridge/buffer.py`

Expected: all commands exit 0.

- [ ] **Step 6: Commit in-memory streams**

```bash
git add apps/node
git commit -m "feat(node): buffer ephemeral Pane output"
```

### Task 5: Implement registration, WSS transport, heartbeat, and reconnect

**Files:**
- Create: `apps/node/src/termflow_node/bridge/transport.py`
- Create: `apps/node/src/termflow_node/bridge/backoff.py`
- Test: `apps/node/tests/test_backoff.py`
- Test: `apps/node/tests/test_bridge_transport.py`

- [ ] **Step 1: Write failing reconnect tests**

```python
import random

from termflow_node.bridge.backoff import ReconnectBackoff


def test_backoff_is_capped_and_resettable() -> None:
    backoff = ReconnectBackoff(base=1, cap=30, rng=random.Random(7))
    delays = [backoff.next_delay() for _ in range(20)]
    assert all(0 <= delay <= 30 for delay in delays)
    backoff.reset()
    assert backoff.attempt == 0
```

- [ ] **Step 2: Run transport tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_backoff.py apps/node/tests/test_bridge_transport.py -q`

Expected: collection FAIL because Bridge transport modules do not exist.

- [ ] **Step 3: Implement idempotent Instance registration**

`ControlPlaneClient.register_instance` posts the local UUID/name using Installation bearer auth.
Persist the returned Instance token atomically before WSS connection. When no token was persisted
because a response was lost, repeat registration for the same UUID; B rotates and returns a new
token only when the same Installation owns that UUID.

- [ ] **Step 4: Implement WSS URL and TLS policy**

Convert `https://host/base` to `wss://host/base/api/v1/bridge/connect` and loopback HTTP to WS.
Send Instance bearer auth in the handshake header, never query parameters. Configure WebSocket
library pings off because TermFlow protocol heartbeats are authoritative. Reject non-loopback
`ws://`.

- [ ] **Step 5: Implement transport loops**

`BridgeTransport.run(handler)` must:

1. register if no Instance token exists;
2. connect WSS;
3. send `bridge.hello` and a full topology before ordinary output;
4. run bounded sender, receiver, and 15-second heartbeat tasks in one `TaskGroup`;
5. cancel siblings when one fails;
6. retain local tmux and buffers;
7. reconnect with full-jitter exponential backoff capped at 30 seconds;
8. reset backoff after a stable successful connection;
9. stop promptly on a shutdown event.

The outbound queue stores at most 256 control/event messages. A full output queue drops the old
output notification, marks that Pane stream as gapped, and never blocks the tmux reader.

- [ ] **Step 6: Test offline startup and reconnect with a scripted WSS server**

Start transport before the test server exists, verify it stays running, then start a local WSS/WS
test server and assert registration, hello, topology, and heartbeats. Force-close it, assert local
state remains, restart it, and assert a new hello/topology arrive. Use injected zero-delay backoff
and fake clock; no real sleeps longer than 50 ms.

- [ ] **Step 7: Run transport tests**

Run: `uv run --package termflow-node pytest apps/node/tests/test_backoff.py apps/node/tests/test_bridge_transport.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit Bridge connectivity**

```bash
git add apps/node
git commit -m "feat(node): connect Instances to Control Plane"
```

### Task 6: Execute only plain-text Pane input with ordering and idempotency

**Files:**
- Create: `apps/node/src/termflow_node/bridge/idempotency.py`
- Create: `apps/node/src/termflow_node/bridge/input_handler.py`
- Modify: `apps/node/src/termflow_node/tmux/runner.py`
- Test: `apps/node/tests/test_idempotency.py`
- Test: `apps/node/tests/test_input_handler.py`
- Test: `apps/node/tests/integration/test_tmux_input.py`

- [ ] **Step 1: Write failing input tests**

```python
import asyncio
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_duplicate_key_writes_once(input_handler, tmux_spy) -> None:
    key = uuid4()
    command = make_input("%1", "继续", submit=True, idempotency_key=key)
    first = await input_handler.handle(command)
    second = await input_handler.handle(command)
    assert first == second
    assert tmux_spy.calls == [("%1", "继续", True)]


@pytest.mark.asyncio
async def test_same_pane_serializes_while_other_pane_runs(input_handler, tmux_spy) -> None:
    await asyncio.gather(
        input_handler.handle(make_input("%1", "a", False, uuid4())),
        input_handler.handle(make_input("%1", "b", False, uuid4())),
        input_handler.handle(make_input("%2", "c", False, uuid4())),
    )
    assert tmux_spy.max_concurrency_by_pane["%1"] == 1
    assert tmux_spy.global_max_concurrency >= 2
```

- [ ] **Step 2: Run input tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_idempotency.py apps/node/tests/test_input_handler.py -q`

Expected: collection FAIL because input modules do not exist.

- [ ] **Step 3: Implement bounded idempotency results**

Use an `OrderedDict[UUID, CommandResultPayload]` with maximum 1024 entries. `get_or_reserve` must
ensure two concurrently duplicated commands share one Future; only the owner executes. Completed
results replace reservations and evict oldest completed entries. Never persist keys or results.

- [ ] **Step 4: Implement shell-free tmux text input**

Validate the Pane exists in the latest topology immediately before execution. Use:

```python
["tmux", "-S", socket, "send-keys", "-t", pane_id, "-l", "--", text]
```

If `submit=True`, issue a second argument-array command:

```python
["tmux", "-S", socket, "send-keys", "-t", pane_id, "Enter"]
```

Never invoke `shell=True`, `eval`, raw tmux command strings, or a special-key field from the wire.
Map a Pane disappearing between validation and send to `pane_not_found`.

- [ ] **Step 5: Implement per-Pane locks and command results**

Maintain one asyncio lock per live Pane. Under that lock, execute text and optional Enter, then
return `command.result` with the original command ID and Idempotency-Key. Remove locks when topology
reports a Pane gone. Errors expose only approved codes and do not echo input text.

- [ ] **Step 6: Verify with real tmux**

Start a Pane running `cat`, inject `hello` with submit, and assert captured Pane output contains
`hello`. The unit spy test remains the authoritative proof that a repeated Idempotency-Key invokes
tmux once, because terminal echo can display one input more than once. Send `"x\x03"` through
protocol construction and assert validation fails before tmux invocation.

Run: `uv run --package termflow-node pytest apps/node/tests/test_idempotency.py apps/node/tests/test_input_handler.py apps/node/tests/integration/test_tmux_input.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit safe input**

```bash
git add apps/node
git commit -m "feat(node): inject idempotent Pane text"
```

### Task 7: Compose the Bridge runtime and output replay path

**Files:**
- Create: `apps/node/src/termflow_node/bridge/runtime.py`
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/src/termflow_node/instances/manager.py`
- Test: `apps/node/tests/test_bridge_runtime.py`
- Test: `apps/node/tests/integration/test_bridge_tmux.py`

- [ ] **Step 1: Write failing runtime tests**

```python
import pytest


@pytest.mark.asyncio
async def test_tmux_output_is_buffered_before_network_publish(bridge_runtime, control_source) -> None:
    control_source.emit_output("%1", b"hello\xff")
    event = await bridge_runtime.next_outbound()
    assert event.type == "pane.output"
    assert event.payload["data_base64"] == "aGVsbG//"
    assert bridge_runtime.buffers.for_pane("%1").total_bytes == 6
```

- [ ] **Step 2: Run runtime tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_bridge_runtime.py -q`

Expected: collection FAIL because the runtime does not exist.

- [ ] **Step 3: Compose tmux reader, topology publisher, and transport**

`BridgeRuntime` owns one control client, output buffers, input handler, and transport. It sends an
initial topology; topology notifications trigger a debounced query and `topology.changed` carrying
the full new snapshot; `%output` appends bytes before enqueueing `pane.output`; `%pause` produces a
gap marker and `capture-pane` snapshot before resuming. Remove buffers/locks for deleted Panes.

- [ ] **Step 4: Implement replay requests**

For `pane.replay_request`, replay buffered chunks after the supplied seq. If unavailable, send
`stream.gap`, call `capture-pane`, reset that Pane's stream, append the snapshot bytes, and send the
new `pane.output`. Reject a request for a nonexistent Pane without creating state.

- [ ] **Step 5: Add the hidden Bridge process entry**

Add an internal Typer command `termflow _bridge --instance-id <uuid>`. It loads only the explicit
Instance state, redirects stdout/stderr to metadata-only logs, installs SIGTERM/SIGINT shutdown,
and runs `BridgeRuntime`. `InstanceManager` starts it using:

```python
[
    sys.executable,
    "-m",
    "termflow_node",
    "_bridge",
    "--instance-id",
    str(instance_id),
]
```

Use `start_new_session=True`, stdin `DEVNULL`, and a private metadata log file. Record the pid only
after verifying the process remains alive through startup validation.

- [ ] **Step 6: Test a real tmux plus fake Control Plane**

Create a private tmux server and runtime, observe initial topology/output, issue a plain input,
receive a success result, force a replay gap, and receive capture snapshot. Assert the Instance
tmux server remains alive after cancelling only Bridge runtime.

Run: `uv run --package termflow-node pytest apps/node/tests/test_bridge_runtime.py apps/node/tests/integration/test_bridge_tmux.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit the composed Bridge**

```bash
git add apps/node
git commit -m "feat(node): compose tmux Bridge runtime"
```

### Task 8: Complete public CLI lifecycle and diagnostics

**Files:**
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/src/termflow_node/instances/manager.py`
- Create: `apps/node/src/termflow_node/diagnostics.py`
- Test: `apps/node/tests/test_cli_lifecycle.py`
- Test: `apps/node/tests/test_diagnostics.py`

- [ ] **Step 1: Write failing CLI behavior tests**

```python
from typer.testing import CliRunner

from termflow_node.cli import app


def test_list_shows_independent_instance_health(instance_factory) -> None:
    first = instance_factory(name="alpha", tmux_alive=True, bridge_alive=True)
    second = instance_factory(name="beta", tmux_alive=True, bridge_alive=False)
    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0
    assert f"{first.id} alpha running connected" in result.stdout
    assert f"{second.id} beta running bridge-down" in result.stdout
```

- [ ] **Step 2: Run CLI tests and confirm failure**

Run: `uv run --package termflow-node pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_diagnostics.py -q`

Expected: tests FAIL because lifecycle commands are incomplete.

- [ ] **Step 3: Implement `new` and `attach`**

`new --name` performs local creation even when B is offline, starts Bridge, then replaces the CLI
process with the tmux attach argv using `os.execvp`. `attach <id-or-name>` resolves exactly one
Instance, verifies tmux, repairs a missing Bridge, then execs attach. Ambiguous names return a
nonzero exit with candidate UUIDs.

- [ ] **Step 4: Implement `list`, `status`, and `kill`**

`list` uses only metadata and process/socket probes; it does not contact B. Both `list` and
`status` emit JSON when `--json` is passed; JSON includes Instance UUID, name, lifecycle, Bridge
health, tmux health, and socket path, but never credentials. `kill` requires an exact UUID or unique name,
SIGTERMs the verified Bridge pid, waits up to five seconds, kills only the explicit tmux server,
and marks the Instance stopped. It reports B notification as best effort, not a blocker.

- [ ] **Step 5: Implement `doctor`**

Check Python, tmux version, directory permissions, login config, each Instance socket, tmux health,
Bridge pid, and server URL TLS policy. Default mode is read-only. `--repair` may chmod only known
TermFlow files to their required modes and restart a missing Bridge for a live tmux server; it must
not kill processes or delete state.

- [ ] **Step 6: Run CLI and diagnostics tests**

Run: `uv run --package termflow-node pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_diagnostics.py -q`

Expected: all tests pass; secret sentinel values are absent from stdout/stderr.

- [ ] **Step 7: Commit the user-facing CLI**

```bash
git add apps/node
git commit -m "feat(node): complete TermFlow Instance CLI"
```

### Task 9: Run the complete Node quality gate

**Files:**
- Modify: `apps/node/tests/conftest.py`
- Create: `apps/node/tests/test_privacy.py`
- Create: `apps/node/tests/integration/test_instance_isolation.py`

- [ ] **Step 1: Add shared real-tmux fixtures**

Fixtures must require tmux 3.2+, allocate a unique temporary `-S` socket, track created server
PIDs, and kill only those explicit servers in `finally`. Fail—not skip—when tmux is absent in the
required CI job. Unit-only jobs may exclude `-m tmux` explicitly.

- [ ] **Step 2: Add multi-Instance isolation test**

Create two private servers and Bridges. Write distinct sentinels to their Pane `%0`, assert each
fake B connection sees only its own sentinel and Instance UUID, then kill the first and assert the
second tmux and Bridge remain alive.

- [ ] **Step 3: Add local privacy regression**

Send `SECRET_NODE_BODY_c8c9` through a Pane, close Bridge cleanly, then search only that test's
TermFlow config/state/log files. Assert the sentinel and raw credentials are absent. The sentinel
may exist only in the process memory assertions and temporary tmux screen during the test.

- [ ] **Step 4: Run all protocol, Control Plane, and Node tests**

```bash
uv run --all-packages pytest packages/protocol/tests apps/control-plane/tests apps/node/tests -q
uv run --all-packages ruff check packages/protocol apps/control-plane apps/node
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
```

Expected: all tests pass and both static checks exit 0.

- [ ] **Step 5: Perform a local CLI smoke test**

Run `uv run --package termflow-node termflow doctor` and then create one temporary test Instance
using a dedicated temporary TermFlow state/config override. Detach with tmux's normal detach key,
run `termflow list`, reattach, and finally run `termflow kill <exact-test-uuid>`.

Expected: the Instance survives detach, appears running, reattaches, and only the explicit test
Instance stops. Do not use or kill the user's default tmux server.

- [ ] **Step 6: Commit verified Node behavior**

```bash
git add apps/node packages/protocol apps/control-plane uv.lock
git commit -m "test(node): verify isolated tmux Bridges"
```
