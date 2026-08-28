# Control Plane Proxy and Resource Bounds Implementation Plan

**Implementation status (2026-08-28):** All repository changes in this plan are implemented and pass `scripts/verify.sh`, including protocol bounds, bridge frame/rate limits, event queue byte budgets, and the real hardened-container verifier.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make client-IP trust deterministic and cap the count, bytes, dimensions, and ingress rate of every currently unbounded topology, bridge, and event-fan-out surface.

**Architecture:** Uvicorn proxy-header rewriting is disabled so the application has one source-resolution policy for HTTP and WebSocket connections. Protocol models reject oversized structured payloads before business logic. The bridge enforces raw frame and byte-rate limits before Pydantic parsing, while event subscribers are bounded by both message count and serialized bytes.

**Tech Stack:** FastAPI, Starlette, Uvicorn, Pydantic 2, asyncio, pytest

---

## Security invariants

- Only `client_source()` interprets `X-Forwarded-For`, and only when `TERMFLOW_TRUST_PROXY=true`; Uvicorn never pre-rewrites `scope["client"]`.
- HTTP and WebSocket authentication/rate-limit paths use the same source-resolution function.
- A topology contains at most 64 windows and 64 panes per window; IDs, names, titles, commands, indexes, dimensions, revisions, capabilities, and binding sets are bounded.
- A bridge frame is at most 256 KiB and sustained inbound bridge traffic is at most 1 MiB/s per connection.
- An event subscriber holds at most 512 messages and 1 MiB of serialized data; exceeding either bound disconnects only that slow subscriber.

### Task 1: Establish one HTTP/WebSocket proxy trust boundary

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/cli.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/rate_limit.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/sessions.py`
- Modify: `apps/control-plane/tests/test_cli.py`
- Modify: `apps/control-plane/tests/test_auth_rate_limit.py`
- Modify: `docs/security.md`

- [ ] **Step 1: Add failing CLI and WebSocket source tests**

In `test_cli.py`, patch `uvicorn.run`, invoke `serve`, and assert:

```python
assert run.call_args.kwargs["proxy_headers"] is False
```

In `test_auth_rate_limit.py`, build `HTTPConnection` scopes for both `"http"` and `"websocket"`. For each type assert:

```python
assert client_source(connection) == "198.51.100.12"  # trust_proxy=True, valid XFF
assert client_source(untrusted_connection) == "127.0.0.1"
assert client_source(malformed_connection) == "127.0.0.1"
```

Add an auth-session WebSocket test proving its limiter receives the forwarded source only when the application setting is enabled.

- [ ] **Step 2: Run the tests and observe failures**

```bash
.envs/dev/bin/python -m pytest \
  apps/control-plane/tests/test_cli.py \
  apps/control-plane/tests/test_auth_rate_limit.py -q
```

Expected: `proxy_headers` is absent and `client_source` accepts `Request`, not a WebSocket/HTTPConnection.

- [ ] **Step 3: Generalize source resolution without changing trust semantics**

In `rate_limit.py`, import `HTTPConnection` from `starlette.requests` and change:

```python
def direct_peer_source(connection: HTTPConnection) -> str:
    return connection.client.host if connection.client is not None else "unknown-peer"


def client_source(connection: HTTPConnection) -> str:
    if not getattr(connection.app.state.settings, "trust_proxy", False):
        return direct_peer_source(connection)
    forwarded = _first_forwarded_address(connection.headers.get("X-Forwarded-For"))
    return forwarded or direct_peer_source(connection)
```

Replace the direct `websocket.client.host` read in `auth/sessions.py` with `client_source(websocket)`.

- [ ] **Step 4: Disable Uvicorn proxy rewriting**

Pass `proxy_headers=False` in the one `uvicorn.run(...)` call in `cli.py`. Do not also configure `forwarded_allow_ips`; the application layer is now the only parser.

- [ ] **Step 5: Document the edge prerequisite**

State that `TERMFLOW_TRUST_PROXY=true` is safe only when the Control Plane is reachable exclusively through a trusted reverse proxy that overwrites, rather than appends to or preserves, `X-Forwarded-For`.

- [ ] **Step 6: Run and commit**

```bash
.envs/dev/bin/python -m pytest \
  apps/control-plane/tests/test_cli.py \
  apps/control-plane/tests/test_auth_rate_limit.py -q
git add -- apps/control-plane/src/termflow_control_plane/cli.py \
  apps/control-plane/src/termflow_control_plane/auth/rate_limit.py \
  apps/control-plane/src/termflow_control_plane/auth/sessions.py \
  apps/control-plane/tests/test_cli.py \
  apps/control-plane/tests/test_auth_rate_limit.py docs/security.md
git commit -m "security: centralize trusted proxy source resolution"
```

### Task 2: Bound topology and variable-size protocol fields

**Files:**
- Modify: `packages/protocol/src/termflow_protocol/topology.py`
- Modify: `packages/protocol/src/termflow_protocol/messages.py`
- Modify: `packages/protocol/tests/test_messages.py`
- Modify: Node fixtures that intentionally exceed the new limits

- [ ] **Step 1: Add boundary and over-limit tests**

Add parameterized tests for each limit. Every test must accept the exact limit and reject limit plus one. At minimum cover:

```python
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_name", "s" * 257),
        ("window_name", "w" * 257),
        ("pane_title", "p" * 257),
        ("current_command", "c" * 257),
    ],
)
def test_topology_rejects_oversized_text(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        build_topology(**{field: value})
```

Also construct 65 windows, 65 panes in one window, 65 capabilities, 129 terminal bindings, index `4096`, dimension `32768`, and revision `2**63`; each must fail validation.

- [ ] **Step 2: Run protocol tests and observe failures**

```bash
.envs/dev/bin/python -m pytest packages/protocol/tests/test_messages.py -q
```

- [ ] **Step 3: Add named protocol limits and constrained aliases**

In `topology.py`, define:

```python
MAX_TOPOLOGY_WINDOWS = 64
MAX_PANES_PER_WINDOW = 64
MAX_TOPOLOGY_TEXT_CHARS = 256
MAX_TMUX_INDEX = 4095
MAX_TMUX_DIMENSION = 32767
MAX_TOPOLOGY_REVISION = 2**63 - 1

SessionId = Annotated[
    str,
    StringConstraints(pattern=r"^\$[0-9]+$", max_length=32),
]
WindowId = Annotated[
    str,
    StringConstraints(pattern=r"^@[0-9]+$", max_length=32),
]
PaneId = Annotated[
    str,
    StringConstraints(pattern=r"^%[0-9]+$", max_length=32),
]
TopologyText = Annotated[str, StringConstraints(max_length=MAX_TOPOLOGY_TEXT_CHARS)]
```

Use `TopologyText` for session/window names, pane title, and optional current command. Add `le=MAX_TMUX_INDEX` to indexes, `le=MAX_TMUX_DIMENSION` to coordinates/dimensions, `le=MAX_TOPOLOGY_REVISION` to revision, and `Field(max_length=...)` to both list fields.

- [ ] **Step 4: Bound remaining protocol collections**

In `messages.py`, define:

```python
MAX_BRIDGE_CAPABILITIES = 64
MAX_CAPABILITY_CHARS = 64
MAX_TERMINAL_BINDINGS = 128
MAX_TERMINAL_DIMENSION = 32767

Capability = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_CAPABILITY_CHARS,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
```

Use `Annotated[tuple[Capability, ...], Field(max_length=MAX_BRIDGE_CAPABILITIES)]` for capabilities and an annotated tuple/list with `Field(max_length=MAX_TERMINAL_BINDINGS)` for terminal bindings. Add dimension upper bounds to terminal opened/size payloads and a 128-character limit to binding key strings.

- [ ] **Step 5: Run protocol plus Node compatibility tests**

```bash
.envs/dev/bin/python -m pytest \
  packages/protocol/tests \
  apps/node/tests/test_topology.py \
  apps/node/tests/test_bridge_transport.py -q
```

- [ ] **Step 6: Commit the protocol boundary**

```bash
git add -- packages/protocol apps/node/tests
git commit -m "security: bound topology and bridge protocol fields"
```

### Task 3: Bound event subscribers by serialized bytes as well as count

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/connections/event_hub.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/tests/test_event_hub.py`
- Modify: `apps/control-plane/tests/test_config.py`
- Modify: all `EventHub(queue_size=...)` test constructors

- [ ] **Step 1: Add a failing byte-budget test**

Add:

```python
@pytest.mark.asyncio
async def test_event_hub_drops_subscriber_when_byte_budget_is_exceeded() -> None:
    hub = EventHub(queue_size=10, queue_max_bytes=300)
    subscriber = await hub.subscribe(instance_id=None)
    first = _event(payload={"data": "a" * 160})
    second = _event(payload={"data": "b" * 160})

    assert await hub.publish(first) == []
    assert await hub.publish(second) == [subscriber.id]
    assert subscriber.closed.is_set()
    assert subscriber.close_code == 4410
```

Add a companion test that `await subscriber.queue.get()` releases the first message's bytes, allowing another message to be queued.

- [ ] **Step 2: Run the focused test and observe constructor failure**

```bash
.envs/dev/bin/python -m pytest apps/control-plane/tests/test_event_hub.py -q
```

- [ ] **Step 3: Implement a byte-counted event queue**

In `event_hub.py`, add a private queue wrapper with the existing consumer API:

```python
class BoundedEventQueue:
    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        self._queue: asyncio.Queue[tuple[WireMessage, int]] = asyncio.Queue(
            maxsize=max_messages
        )
        self._max_bytes = max_bytes
        self._queued_bytes = 0

    def put_nowait(self, message: WireMessage) -> None:
        size = len(message.model_dump_json().encode("utf-8"))
        if size > self._max_bytes - self._queued_bytes:
            raise asyncio.QueueFull
        self._queue.put_nowait((message, size))
        self._queued_bytes += size

    async def get(self) -> WireMessage:
        message, size = await self._queue.get()
        self._queued_bytes -= size
        return message

    def empty(self) -> bool:
        return self._queue.empty()
```

Change `EventSubscriber.queue` to `BoundedEventQueue`. Change `EventHub.__init__` to require `queue_size` and `queue_max_bytes`, construct the wrapper in `subscribe`, and preserve the current drop-on-`QueueFull` behavior.

- [ ] **Step 4: Add and wire the setting**

In `Settings` add:

```python
event_queue_max_bytes: int = Field(default=1024 * 1024, ge=1, le=16 * 1024 * 1024)
```

Pass it from `app.py`. Update every test constructor to supply an explicit small byte budget, usually `queue_max_bytes=1024 * 1024`.

- [ ] **Step 5: Run and commit**

```bash
.envs/dev/bin/python -m pytest \
  apps/control-plane/tests/test_event_hub.py \
  apps/control-plane/tests/test_browser_sessions.py \
  apps/control-plane/tests/test_epoch_boundaries.py \
  apps/control-plane/tests/test_heartbeat.py \
  apps/control-plane/tests/test_config.py -q
git add -- apps/control-plane/src/termflow_control_plane/connections/event_hub.py \
  apps/control-plane/src/termflow_control_plane/config.py \
  apps/control-plane/src/termflow_control_plane/app.py \
  apps/control-plane/tests
git commit -m "security: enforce event subscriber byte budgets"
```

### Task 4: Enforce bridge raw-frame and byte-rate limits before parsing

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/connections/token_bucket.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/terminal.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/bridge.py`
- Modify: `apps/control-plane/src/termflow_control_plane/cli.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/tests/test_bridge_websocket.py`
- Modify: `apps/control-plane/tests/test_config.py`

- [ ] **Step 1: Add WebSocket tests for both rejection modes**

Using the existing authenticated bridge fixture, override settings and assert:

```python
def test_bridge_rejects_frame_above_raw_byte_limit(...) -> None:
    settings.bridge_max_frame_bytes = 128
    with client.websocket_connect(bridge_url, headers=headers) as websocket:
        websocket.send_text("x" * 129)
        assert websocket.receive()["code"] == 1009


def test_bridge_rate_limits_sustained_input_bytes(...) -> None:
    settings.bridge_input_rate_bytes_per_second = 64
    with client.websocket_connect(bridge_url, headers=headers) as websocket:
        websocket.send_text(valid_message_of_48_bytes)
        websocket.send_text(valid_message_of_48_bytes)
        assert websocket.receive()["code"] == 4429
```

Use the existing fake clock pattern from terminal tests so the rate test is deterministic and never sleeps.

- [ ] **Step 2: Extract the tested token bucket**

Move the terminal `_TokenBucket` into `connections/token_bucket.py` as `TokenBucket`, preserving its `create(rate)` and `consume(amount)` behavior. Update terminal imports and run terminal tests before changing bridge behavior:

```bash
.envs/dev/bin/python -m pytest apps/control-plane/tests/test_terminal_websocket.py -q
```

- [ ] **Step 3: Add bounded settings**

Add:

```python
bridge_max_frame_bytes: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
bridge_input_rate_bytes_per_second: int = Field(
    default=1024 * 1024,
    ge=1024,
    le=16 * 1024 * 1024,
)
```

Set Uvicorn `ws_max_size=max(settings.terminal_max_frame_bytes, settings.bridge_max_frame_bytes)` so the server transport never permits more than the larger explicitly configured application cap.

- [ ] **Step 4: Read and measure bridge frames before model validation**

Replace `receive_text()` with `receive()` and accept text frames only. Encode text once to UTF-8, then apply limits in this order:

```python
incoming = await websocket.receive()
text = incoming.get("text")
if text is None:
    await websocket.close(code=1003, reason="Text frames required")
    return
raw = text.encode("utf-8")
if len(raw) > settings.bridge_max_frame_bytes:
    await websocket.close(code=1009, reason="Bridge frame too large")
    return
if not bucket.consume(len(raw)):
    await websocket.close(code=4429, reason="Bridge input rate exceeded")
    return
message = WireMessage.model_validate_json(raw)
```

Create one bucket per accepted bridge connection and pass `Settings` into `_receive_messages` explicitly. Do not use a process-global bucket.

- [ ] **Step 5: Run bridge and terminal regression tests**

```bash
.envs/dev/bin/python -m pytest \
  apps/control-plane/tests/test_bridge_websocket.py \
  apps/control-plane/tests/test_terminal_websocket.py \
  apps/control-plane/tests/test_config.py -q
```

- [ ] **Step 6: Commit ingress limiting**

```bash
git add -- apps/control-plane
git commit -m "security: cap bridge frames and inbound byte rate"
```

### Task 5: Wire production defaults, document limits, and verify

**Files:**
- Modify: `deploy/compose.yaml`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `docs/security.md`
- Modify: `docs/operations.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Add failing deployment/documentation contracts**

Require these Compose environment values:

```yaml
TERMFLOW_BRIDGE_MAX_FRAME_BYTES: "262144"
TERMFLOW_BRIDGE_INPUT_RATE_BYTES_PER_SECOND: "1048576"
TERMFLOW_EVENT_QUEUE_MAX_BYTES: "1048576"
```

Require the security guide to list topology count limits, field-length limits, bridge frame/rate limits, and event count/byte limits.

- [ ] **Step 2: Add exact Compose values and operational guidance**

Add the environment keys above. Explain that increasing one bound requires a memory-budget review and a boundary test at the new maximum plus one.

- [ ] **Step 3: Run focused and full verification**

```bash
.envs/dev/bin/python -m pytest \
  packages/protocol/tests \
  apps/control-plane/tests \
  tests/deploy/test_compose_contract.py \
  tests/docs/test_documentation_contract.py -q
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH ./scripts/verify.sh
```

Expected: all tests pass; `docker compose -f deploy/compose.yaml config` renders the three exact security settings.

- [ ] **Step 4: Commit configuration and documentation**

```bash
git add -- deploy/compose.yaml docs/security.md docs/operations.md \
  tests/deploy/test_compose_contract.py tests/docs/test_documentation_contract.py
git commit -m "docs: publish Control Plane resource ceilings"
```
