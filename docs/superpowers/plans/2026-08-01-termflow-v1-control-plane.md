# TermFlow V1 Protocol and Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared V1 protocol and a single-worker FastAPI Control Plane that securely enrolls installations, registers live Instances, exposes topology, routes one plain-text Pane input at a time, and forwards ephemeral events without storing terminal content.

**Architecture:** A Pydantic-only workspace package owns all HTTP and WebSocket contracts. The Control Plane uses SQLAlchemy repositories for identity and metadata, an in-memory registry for live Bridge connections and topology, and bounded asyncio queues for command and event routing; SQLite never stores Pane input or output.

**Tech Stack:** Python 3.12, uv, Pydantic 2, FastAPI, Uvicorn, SQLAlchemy 2, aiosqlite, pydantic-settings, Typer, pytest, pytest-asyncio, HTTPX.

---

## File map

Create these focused units:

- `pyproject.toml`: uv workspace membership and shared tool configuration.
- `packages/protocol/src/termflow_protocol/{common,topology,messages,http}.py`: versioned wire types and HTTP DTOs only.
- `apps/control-plane/src/termflow_control_plane/config.py`: environment-backed runtime settings.
- `apps/control-plane/src/termflow_control_plane/persistence/{database,models,repositories}.py`: SQLite schema and transactions.
- `apps/control-plane/src/termflow_control_plane/auth/tokens.py`: high-entropy token issue/hash/compare helpers.
- `apps/control-plane/src/termflow_control_plane/connections/{registry,event_hub}.py`: live state and bounded subscribers.
- `apps/control-plane/src/termflow_control_plane/routing/router.py`: command correlation, timeout, and audit metadata.
- `apps/control-plane/src/termflow_control_plane/api/{dependencies,enrollment,instances,bridge,events}.py`: transport adapters.
- `apps/control-plane/src/termflow_control_plane/{app,cli}.py`: application factory and operator CLI.

### Task 1: Bootstrap the uv workspace and importable packages

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `packages/protocol/pyproject.toml`
- Create: `packages/protocol/src/termflow_protocol/__init__.py`
- Create: `apps/control-plane/pyproject.toml`
- Create: `apps/control-plane/src/termflow_control_plane/__init__.py`
- Create: `apps/node/pyproject.toml`
- Create: `apps/node/src/termflow_node/__init__.py`
- Create: `apps/clients/README.md`
- Test: `packages/protocol/tests/test_package.py`

- [ ] **Step 1: Write the failing package test**

```python
from termflow_protocol import PROTOCOL_VERSION


def test_protocol_version_is_one() -> None:
    assert PROTOCOL_VERSION == 1
```

- [ ] **Step 2: Run the test before scaffolding**

Run: `python -m pytest packages/protocol/tests/test_package.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'termflow_protocol'`.

- [ ] **Step 3: Create the workspace manifests**

Use this root configuration:

```toml
[tool.uv.workspace]
members = ["packages/protocol", "apps/control-plane", "apps/node"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["packages/protocol/tests", "apps/control-plane/tests", "apps/node/tests", "tests"]
markers = ["tmux: requires a real tmux binary", "e2e: full cross-process test"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.12"
strict = true
```

Use `hatchling` build backends. Name the packages `termflow-protocol`,
`termflow-control-plane`, and `termflow-node`. Add these runtime dependencies:

```toml
# packages/protocol
dependencies = ["pydantic>=2.10,<3"]

# apps/control-plane
dependencies = [
  "termflow-protocol",
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.34,<1",
  "sqlalchemy>=2.0,<3",
  "aiosqlite>=0.20,<1",
  "pydantic-settings>=2.7,<3",
  "typer>=0.15,<1",
]

# apps/node; its implementation is Plan 2
dependencies = [
  "termflow-protocol",
  "httpx>=0.28,<1",
  "websockets>=14,<17",
  "pydantic-settings>=2.7,<3",
  "platformdirs>=4,<5",
  "typer>=0.15,<1",
]
```

Declare `termflow-protocol` as a uv workspace source in both app manifests. Add
`pytest>=8,<10`, `pytest-asyncio>=0.24,<2`, `ruff>=0.9,<1`, and `mypy>=1.14,<2` to all three
packages' `dependency-groups.dev`. Add `httpx>=0.28,<1` to the Control Plane dev group because its
FastAPI tests use TestClient. Add this protocol constant:

```python
PROTOCOL_VERSION = 1
```

The `.gitignore` must contain:

```gitignore
.venv/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.py[cod]
*.db
*.db-shm
*.db-wal
.env
dist/
```

The clients README must state: `TermFlow V1 intentionally contains no client implementation.`

- [ ] **Step 4: Lock dependencies and run the package test**

Run: `uv lock && uv run --package termflow-protocol pytest packages/protocol/tests/test_package.py -q`

Expected: `1 passed`.

- [ ] **Step 5: Run formatting and type checks**

Run: `uv run --package termflow-protocol ruff check packages/protocol && uv run --package termflow-protocol mypy packages/protocol/src`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the scaffold**

```bash
git add pyproject.toml uv.lock .gitignore packages apps
git commit -m "build: scaffold TermFlow workspace"
```

### Task 2: Define the versioned protocol and input validation

**Files:**
- Create: `packages/protocol/src/termflow_protocol/common.py`
- Create: `packages/protocol/src/termflow_protocol/topology.py`
- Create: `packages/protocol/src/termflow_protocol/messages.py`
- Create: `packages/protocol/src/termflow_protocol/http.py`
- Modify: `packages/protocol/src/termflow_protocol/__init__.py`
- Test: `packages/protocol/tests/test_messages.py`
- Test: `packages/protocol/tests/test_http_models.py`

- [ ] **Step 1: Write failing protocol tests**

```python
from uuid import uuid4

import pytest
from pydantic import ValidationError

from termflow_protocol import PaneInputRequest, PaneOutputPayload, WireMessage


def test_plain_input_rejects_control_characters() -> None:
    with pytest.raises(ValidationError):
        PaneInputRequest(text="hello\x03", submit=False)


def test_plain_input_requires_text_or_submit() -> None:
    with pytest.raises(ValidationError):
        PaneInputRequest(text="", submit=False)


def test_output_bytes_round_trip_as_base64() -> None:
    raw = b"\xff\x1b[31mred"
    payload = PaneOutputPayload.from_bytes("%1", uuid4(), 7, raw)
    assert payload.to_bytes() == raw


def test_unknown_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WireMessage(
            protocol_version=2,
            message_id=uuid4(),
            type="bridge.heartbeat",
            instance_id=uuid4(),
            payload={},
        )
```

- [ ] **Step 2: Run the protocol tests and confirm failure**

Run: `uv run --package termflow-protocol pytest packages/protocol/tests/test_messages.py packages/protocol/tests/test_http_models.py -q`

Expected: collection FAIL because the models do not exist.

- [ ] **Step 3: Implement common and topology models**

Define `WireMessage` with `protocol_version: Literal[1]`, UUID message and Instance IDs,
UTC `sent_at`, a `MessageType` literal union, and `payload: dict[str, object]`. Define:

```python
class PaneSnapshot(BaseModel):
    pane_id: str
    window_id: str
    index: int
    title: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    active: bool
    dead: bool


class WindowSnapshot(BaseModel):
    window_id: str
    index: int
    name: str
    active: bool
    panes: list[PaneSnapshot]


class TopologySnapshot(BaseModel):
    session_id: str
    session_name: str
    revision: int = Field(ge=0)
    windows: list[WindowSnapshot]
```

IDs must match tmux ID forms (`$<digits>`, `@<digits>`, `%<digits>`) through anchored regular
expressions.

- [ ] **Step 4: Implement payload and HTTP models**

Define typed payloads for `bridge.hello`, `bridge.heartbeat`, `topology.snapshot`,
`topology.changed`, `pane.output`, `pane.input`, `pane.replay_request`, `stream.gap`, and
`command.result`. Also define metadata-only `instance.online` and `instance.offline` event
payloads containing Instance ID and event time. Base64 handling must use strict decoding:

```python
class PaneOutputPayload(BaseModel):
    pane_id: str
    stream_id: UUID
    seq: int = Field(ge=1)
    data_base64: str
    captured_at: datetime

    @classmethod
    def from_bytes(cls, pane_id: str, stream_id: UUID, seq: int, data: bytes) -> "PaneOutputPayload":
        return cls(
            pane_id=pane_id,
            stream_id=stream_id,
            seq=seq,
            data_base64=base64.b64encode(data).decode("ascii"),
            captured_at=datetime.now(UTC),
        )

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)
```

Define `PaneInputRequest` with a 16 KiB UTF-8 limit, no C0/C1/DEL control characters, and a
model validator requiring non-empty text or `submit=True`. Define `ErrorEnvelope`,
`InstanceResponse`, `InstanceListResponse`, `TopologyResponse`, `CommandResponse`,
`EnrollmentCreateResponse`, `InstallationEnrollRequest`, `InstallationEnrollResponse`,
`InstanceRegisterRequest`, and `InstanceRegisterResponse`.

- [ ] **Step 5: Re-export the public protocol surface**

`termflow_protocol/__init__.py` must expose only the constant, public topology types, payload
types, and HTTP DTOs used by A/B/C. Do not export transport or persistence helpers.

- [ ] **Step 6: Run tests and static checks**

Run: `uv run --package termflow-protocol pytest packages/protocol/tests -q && uv run --package termflow-protocol ruff check packages/protocol && uv run --package termflow-protocol mypy packages/protocol/src`

Expected: all tests pass and both static checks exit 0.

- [ ] **Step 7: Commit the protocol**

```bash
git add packages/protocol
git commit -m "feat(protocol): define TermFlow V1 wire contracts"
```

### Task 3: Add Control Plane configuration, database schema, and token helpers

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/config.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/__init__.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/tokens.py`
- Create: `apps/control-plane/src/termflow_control_plane/persistence/__init__.py`
- Create: `apps/control-plane/src/termflow_control_plane/persistence/database.py`
- Create: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Create: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Test: `apps/control-plane/tests/test_tokens.py`
- Test: `apps/control-plane/tests/test_repositories.py`

- [ ] **Step 1: Write failing token and repository tests**

```python
from datetime import UTC, datetime, timedelta

import pytest

from termflow_control_plane.auth.tokens import hash_token, issue_token, token_matches


def test_issued_token_is_high_entropy_and_hash_matches() -> None:
    token = issue_token()
    assert len(token) >= 43
    assert token_matches(token, hash_token(token))
    assert not token_matches(token + "x", hash_token(token))


@pytest.mark.asyncio
async def test_enrollment_is_consumed_once(repositories) -> None:
    raw = "x" * 43
    enrollment = await repositories.enrollments.create(
        hash_token(raw), datetime.now(UTC) + timedelta(minutes=10)
    )
    assert await repositories.enrollments.consume(hash_token(raw)) == enrollment.id
    assert await repositories.enrollments.consume(hash_token(raw)) is None
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_tokens.py apps/control-plane/tests/test_repositories.py -q`

Expected: collection FAIL because auth and persistence modules do not exist.

- [ ] **Step 3: Implement settings and tokens**

Use `BaseSettings` with prefix `TERMFLOW_` and fields:

```python
class Settings(BaseSettings):
    admin_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/termflow.db"
    allow_insecure_loopback: bool = False
    heartbeat_interval_seconds: int = 15
    offline_after_seconds: int = 45
    command_timeout_seconds: float = 5.0
    connection_queue_size: int = 256
    event_queue_size: int = 512
    max_input_bytes: int = 16 * 1024
```

Validate `offline_after_seconds > heartbeat_interval_seconds`. Implement tokens with
`secrets.token_urlsafe(32)`, SHA-256, and `hmac.compare_digest`.

- [ ] **Step 4: Implement the metadata-only schema**

Create SQLAlchemy 2 async models `EnrollmentToken`, `Installation`, `Instance`, and
`AuditEvent`. `AuditEvent` may contain `operation`, `input_bytes`, IDs, timestamps, result, and
error code; it must not have columns named `text`, `input`, `output`, `payload`, `content`, or
`data`. Add unique indexes for token hashes and Instance UUIDs.

- [ ] **Step 5: Implement atomic repositories**

Provide repository methods:

```python
await enrollments.create(token_hash, expires_at)
await enrollments.consume(token_hash, now=datetime.now(UTC))
await installations.create(token_hash)
await installations.get_by_token_hash(token_hash)
await instances.register_or_rotate(instance_id, installation_id, name, token_hash)
await instances.get(instance_id)
await instances.get_by_token_hash(token_hash)
await instances.list_all()
await audit.record(operation, instance_id, pane_id, input_bytes, result, error_code)
```

`consume` must update `used_at` in the same transaction that verifies unused and unexpired.
`register_or_rotate` must create a first registration, rotate only for the owning Installation,
and reject an existing UUID owned by another Installation in one transaction.
The database fixture must use a temporary SQLite file, enable WAL, and create/drop schema.

- [ ] **Step 6: Run repository, privacy, and static checks**

Add an assertion that the Audit table column set is exactly the approved metadata fields. Run:

`uv run --package termflow-control-plane pytest apps/control-plane/tests/test_tokens.py apps/control-plane/tests/test_repositories.py -q && uv run --package termflow-control-plane ruff check apps/control-plane && uv run --package termflow-control-plane mypy apps/control-plane/src`

Expected: tests pass; static checks exit 0.

- [ ] **Step 7: Commit persistence and auth primitives**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): add identity persistence"
```

### Task 4: Implement enrollment and Installation authentication

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/api/__init__.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/enrollment.py`
- Create: `apps/control-plane/src/termflow_control_plane/errors.py`
- Create: `apps/control-plane/src/termflow_control_plane/app.py`
- Create: `apps/control-plane/tests/conftest.py`
- Test: `apps/control-plane/tests/test_enrollment_api.py`

- [ ] **Step 1: Write failing API tests**

```python
def test_admin_creates_and_installation_consumes_enrollment(client, admin_headers) -> None:
    issued = client.post("/api/v1/enrollment-tokens", headers=admin_headers)
    assert issued.status_code == 201
    raw = issued.json()["token"]

    enrolled = client.post("/api/v1/installations/enroll", json={"enrollment_token": raw})
    assert enrolled.status_code == 201
    assert enrolled.json()["installation_token"]

    replay = client.post("/api/v1/installations/enroll", json={"enrollment_token": raw})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_enrollment_token"


def test_admin_route_rejects_missing_token(client) -> None:
    response = client.post("/api/v1/enrollment-tokens")
    assert response.status_code == 401
```

- [ ] **Step 2: Run the API tests and confirm failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_enrollment_api.py -q`

Expected: collection FAIL because the application factory does not exist.

- [ ] **Step 3: Implement dependencies and uniform errors**

Create dependencies for settings, repositories, admin bearer auth, Installation bearer auth,
and request IDs. Define a `TermFlowError(code, status_code, message)` exception and a handler
that always emits:

```python
{"error": {"code": exc.code, "message": exc.message, "request_id": request_id}}
```

Use constant-time comparison for the admin token. Never include a supplied token in an error.

- [ ] **Step 4: Implement enrollment endpoints**

`POST /api/v1/enrollment-tokens` requires Admin Token, creates a ten-minute single-use token,
and returns it once. `POST /api/v1/installations/enroll` consumes it atomically, creates an
Installation Credential, stores only its hash, and returns the raw credential once.

- [ ] **Step 5: Build the application factory**

`create_app(settings, repositories)` must install lifespan database initialization, request ID
middleware, exception handlers, and the enrollment router. Add unauthenticated `GET /healthz`
returning exactly `{"status":"ok"}` without database, token, or connection details. Tests inject
temporary settings and repositories; production construction reads environment settings.

- [ ] **Step 6: Run API and privacy tests**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_enrollment_api.py apps/control-plane/tests/test_repositories.py -q`

Expected: tests pass and captured logs do not contain either raw token.

- [ ] **Step 7: Commit enrollment**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): add secure installation enrollment"
```

### Task 5: Build the live Instance registry and Bridge WebSocket

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/connections/__init__.py`
- Create: `apps/control-plane/src/termflow_control_plane/connections/registry.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/bridge.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Test: `apps/control-plane/tests/test_registry.py`
- Test: `apps/control-plane/tests/test_bridge_websocket.py`

- [ ] **Step 1: Write failing registry tests**

```python
import asyncio
from uuid import uuid4

import pytest

from termflow_control_plane.connections.registry import InstanceOffline, LiveInstanceRegistry


@pytest.mark.asyncio
async def test_register_replaces_old_connection_and_marks_old_closed() -> None:
    registry = LiveInstanceRegistry(queue_size=2)
    instance_id = uuid4()
    first = await registry.register(instance_id)
    second = await registry.register(instance_id)
    assert first.replaced.is_set()
    assert await registry.get(instance_id) is second


@pytest.mark.asyncio
async def test_offline_instance_fails_without_queueing() -> None:
    registry = LiveInstanceRegistry(queue_size=1)
    with pytest.raises(InstanceOffline):
        await registry.enqueue(uuid4(), object())
```

- [ ] **Step 2: Run registry tests and confirm failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_registry.py -q`

Expected: collection FAIL because the registry does not exist.

- [ ] **Step 3: Implement bounded live connections**

`LiveConnection` must hold `instance_id`, a bounded `asyncio.Queue[WireMessage]`, the latest
`TopologySnapshot | None`, `last_heartbeat`, `pending: dict[UUID, Future[CommandResultPayload]]`,
and a `replaced` event. `LiveInstanceRegistry` must atomically register, unregister only the
matching connection object, get, enqueue with `put_nowait`, list online IDs, and expire stale
connections. Queue full maps to `backpressure`; missing maps to `instance_offline`.

- [ ] **Step 4: Implement Instance registration**

Add `POST /api/v1/instances/register`, authenticated by Installation Credential. It accepts a
locally generated UUID and name. First registration issues an Instance credential. A retry for the
same UUID by the same Installation atomically rotates and returns a new Instance credential so a
lost first HTTP response cannot strand the local Instance. A repeated UUID under another
Installation is forbidden. Store only the current credential hash.

- [ ] **Step 5: Implement Bridge WSS authentication and loops**

Add `/api/v1/bridge/connect`. Authenticate the Instance bearer token before `accept()`. Run one
sender task that drains the bounded queue and one receiver task that validates `WireMessage`.
Handle:

- `bridge.hello`: verify the payload Instance matches the credential;
- `bridge.heartbeat`: update `last_heartbeat`;
- `topology.snapshot` and `topology.changed`: replace the in-memory topology;
- `pane.output` and `stream.gap`: call an injected async `publish_event` callback; wire a no-op
  callback in this task so Bridge connectivity is independently testable, then wire the bounded
  EventHub in Task 7;
- `command.result`: resolve and remove the matching pending Future.

On disconnect, unregister only that socket's `LiveConnection` and fail pending Futures with
`connection_lost`. Do not mutate tmux or persist messages.

- [ ] **Step 6: Test authentication, topology, and replacement**

Use FastAPI's WebSocket test client with a registered Instance token. Assert an invalid token is
rejected, a valid hello plus topology becomes visible in the registry, and a second connection
replaces the first without unregistering the second when the first exits.

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_registry.py apps/control-plane/tests/test_bridge_websocket.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit live Bridge connectivity**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): register live Bridge connections"
```

### Task 6: Add Instance reads and confirmed plain-text input routing

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/routing/__init__.py`
- Create: `apps/control-plane/src/termflow_control_plane/routing/router.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/instances.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Test: `apps/control-plane/tests/test_instance_api.py`
- Test: `apps/control-plane/tests/test_router.py`

- [ ] **Step 1: Write failing routing tests**

```python
from uuid import uuid4

import pytest

from termflow_control_plane.errors import TermFlowError


@pytest.mark.asyncio
async def test_input_waits_for_matching_bridge_confirmation(router, live_connection) -> None:
    pane_id = "%1"
    task = asyncio.create_task(
        router.send_input(live_connection.instance_id, pane_id, "继续", True, uuid4())
    )
    message = await live_connection.outbound.get()
    assert message.type == "pane.input"
    assert message.payload["pane_id"] == pane_id
    router.resolve_result(live_connection, message.payload["command_id"], ok=True)
    assert (await task).ok is True


@pytest.mark.asyncio
async def test_unknown_pane_is_rejected_before_enqueue(router, live_connection) -> None:
    with pytest.raises(TermFlowError) as caught:
        await router.send_input(live_connection.instance_id, "%999", "x", False, uuid4())
    assert caught.value.code == "pane_not_found"
```

- [ ] **Step 2: Run routing tests and confirm failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_router.py -q`

Expected: collection FAIL because the router does not exist.

- [ ] **Step 3: Implement the command router**

`CommandRouter.send_input` must:

1. require an online connection;
2. require the Pane ID in the current topology;
3. build a `pane.input` message with a UUID command ID and caller Idempotency-Key;
4. add a Future to the connection before enqueueing;
5. await it with `asyncio.timeout(settings.command_timeout_seconds)`;
6. remove the Future in `finally`;
7. map timeout to `command_timeout`, disconnection to `outcome_unknown`, and a negative Bridge
   result to its approved error code;
8. write only byte count and result to audit.

- [ ] **Step 4: Implement Instance REST routes**

All routes require Admin Token. Implement list, detail, online-only topology, and Pane input.
Require UUID `Idempotency-Key`. Return `CommandResponse` only after Bridge confirmation. Offline
topology/input returns `409 instance_offline`; unknown Instance returns 404; unknown Pane returns
404; queue full returns 429.

- [ ] **Step 5: Add HTTP integration tests with a fake Bridge**

Open a Bridge WebSocket, send a topology containing `%1`, call the input endpoint in a parallel
thread/task, inspect the WSS `pane.input`, send `command.result`, and assert HTTP 200. Also assert
control characters return 422 before any WSS message, and an offline Instance returns 409 without
creating an Audit row containing text.

- [ ] **Step 6: Run the routing and API suite**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_router.py apps/control-plane/tests/test_instance_api.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit routed input**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): route confirmed Pane input"
```

### Task 7: Implement ephemeral event subscriptions and heartbeat expiry

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/connections/event_hub.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/events.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/bridge.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Test: `apps/control-plane/tests/test_event_hub.py`
- Test: `apps/control-plane/tests/test_events_websocket.py`
- Test: `apps/control-plane/tests/test_heartbeat.py`

- [ ] **Step 1: Write failing bounded-subscriber tests**

```python
import pytest

from termflow_control_plane.connections.event_hub import EventHub


@pytest.mark.asyncio
async def test_slow_subscriber_is_removed_without_blocking_publish() -> None:
    hub = EventHub(queue_size=1)
    subscriber = await hub.subscribe(instance_id=None)
    await hub.publish(object())
    dropped = await hub.publish(object())
    assert dropped == [subscriber.id]
    assert subscriber.closed.is_set()
```

- [ ] **Step 2: Run event tests and confirm failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_event_hub.py -q`

Expected: collection FAIL because `EventHub` does not exist.

- [ ] **Step 3: Implement the event hub**

Subscribers have a UUID, bounded queue, optional Instance filter, and closed event. `publish` uses
`put_nowait`; full subscribers are removed and closed without waiting, and their IDs are returned
to the caller for metadata-only logging. `unsubscribe` is idempotent. The hub stores no replay
history and never raises because one subscriber is slow.

- [ ] **Step 4: Implement `/api/v1/events`**

Authenticate Admin Token during the WebSocket handshake. Require an `instance_id` filter and
accept an optional replay cursor consisting of `pane_id`, `stream_id`, and `after_seq`; all three
cursor fields must be present together. Subscribe, then enqueue one `pane.replay_request` to the
live Bridge when a cursor is supplied. Send validated WireMessage JSON and always unsubscribe in
`finally`. Do not accept client-sent commands on this WSS. Bridge receiver publishes only topology,
output, gap, and online/offline events.

- [ ] **Step 5: Add heartbeat expiry in application lifespan**

Run a background loop once per second. It calls `registry.expire_before(now - offline_after)` and
publishes `instance.offline` for expired connections. Cancellation during shutdown must be clean.
Use an injectable clock in tests to avoid sleeping 45 seconds.

- [ ] **Step 6: Test live delivery, filtering, slow consumers, and expiry**

Assert an output event from a fake Bridge reaches a matching subscriber, not a different Instance
filter; filling a subscriber queue does not block Bridge processing; advancing the fake clock
expires an Instance and fails its pending command.

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_event_hub.py apps/control-plane/tests/test_events_websocket.py apps/control-plane/tests/test_heartbeat.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit event delivery**

```bash
git add apps/control-plane
git commit -m "feat(control-plane): stream ephemeral Instance events"
```

### Task 8: Add the operator CLI and complete Control Plane verification

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/cli.py`
- Create: `apps/control-plane/src/termflow_control_plane/__main__.py`
- Modify: `apps/control-plane/pyproject.toml`
- Test: `apps/control-plane/tests/test_cli.py`
- Test: `apps/control-plane/tests/test_privacy.py`

- [ ] **Step 1: Write failing CLI tests**

```python
from typer.testing import CliRunner

from termflow_control_plane.cli import app


def test_enrollment_create_prints_token_once(configured_database) -> None:
    result = CliRunner().invoke(app, ["enrollment", "create"])
    assert result.exit_code == 0
    token = result.stdout.strip()
    assert len(token) >= 43
    assert configured_database.raw_bytes().count(token.encode()) == 0
```

- [ ] **Step 2: Run CLI tests and confirm failure**

Run: `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_cli.py -q`

Expected: collection FAIL because the CLI does not exist.

- [ ] **Step 3: Implement the operator CLI**

Expose `termflow-control serve` and `termflow-control enrollment create`. `serve` must start one
Uvicorn worker and reject a configured worker count other than one. `enrollment create` opens the
same repository, writes a ten-minute enrollment hash, and prints the raw token exactly once.

Add this script entry:

```toml
[project.scripts]
termflow-control = "termflow_control_plane.cli:app"
```

- [ ] **Step 4: Add privacy regression tests**

Capture application logs, query SQLite schema/rows, send the sentinel text
`SECRET_TERMINAL_BODY_9f0d`, and assert the sentinel appears only in the fake Bridge's received
message—not in logs, Audit rows, or any SQLite file bytes after checkpoint. Assert raw enrollment,
Installation, and Instance tokens are also absent from logs and database bytes.

- [ ] **Step 5: Run the full Control Plane quality gate**

```bash
uv run --package termflow-control-plane pytest packages/protocol/tests apps/control-plane/tests -q
uv run --package termflow-control-plane ruff check packages/protocol apps/control-plane
uv run --package termflow-control-plane mypy packages/protocol/src apps/control-plane/src
```

Expected: all tests pass; Ruff and mypy exit 0.

- [ ] **Step 6: Smoke-start the API**

Run with a temporary data directory and loopback-only insecure mode:

```bash
TERMFLOW_ADMIN_TOKEN=test-admin-token-which-is-long-enough \
TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////tmp/termflow-control-plane-smoke.db \
TERMFLOW_ALLOW_INSECURE_LOOPBACK=true \
uv run --package termflow-control-plane termflow-control serve --host 127.0.0.1 --port 8765
```

In another terminal run `curl -fsS http://127.0.0.1:8765/healthz`.

Expected: `{"status":"ok"}`. Stop the server with Ctrl+C and remove only the explicit smoke DB
files under `/tmp`.

- [ ] **Step 7: Commit the verified Control Plane**

```bash
git add apps/control-plane packages/protocol pyproject.toml uv.lock
git commit -m "feat(control-plane): complete TermFlow V1 control service"
```
