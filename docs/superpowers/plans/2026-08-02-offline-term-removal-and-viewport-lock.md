
# Offline Term Removal and Viewport Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permanently remove offline Term registrations without touching local tmux, require explicit local reactivation after credential rejection, and make terminal viewport locking and mobile page containment deterministic across desktop and mobile.

**Architecture:** The Control Plane owns a hard-delete plus an in-memory retirement gate so an offline deletion wins against later reconnects. The Node persists schema-v3 `remote_access` state and exposes an explicit activation transaction; the shared Vue client owns deletion UI, viewport locking, and route-scoped page containment while Web C remains a thin browser adapter.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, Typer, httpx, websockets, Vue 3, TypeScript, Vitest, Playwright, xterm.js, Docker Compose.

---

## Starting point and execution rules

- Repository root: `/home/mcocdaa/AI_CODE/TermFlow`
- Approved design: `docs/superpowers/specs/2026-08-02-offline-term-removal-and-viewport-lock-design.md`
- Plan written against clean `main` at `ea7c3ea` on 2026-08-02. Re-run `git status --short --branch` and `git log -1 --oneline` before execution because this is a shared workspace.
- Use `superpowers:using-git-worktrees` before implementation. Execute this plan in an isolated worktree, never over unrelated shared-worktree changes.
- Stage only the files named by the current task. Each task starts with a failing test, ends with focused green evidence, and produces one small commit.
- Do not deploy until Task 15's complete verification and isolated real-browser run pass.
- Report implementation, tests, isolated browser acceptance, Docker deployment, and deployed UI smoke as separate evidence.

## File responsibility map

### Control Plane

- `apps/control-plane/src/termflow_control_plane/connections/registry.py`: blocks reconnects once offline retirement begins; clears the block only after installation-authenticated registration.
- `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`: exact-ID hard deletion of an Instance row.
- `apps/control-plane/src/termflow_control_plane/api/terms.py`: administrator DELETE contract, online/offline decision, audit, and persistence-failure rollback.
- `apps/control-plane/src/termflow_control_plane/api/instances.py`: clear retirement after a fresh credential is committed.
- `apps/control-plane/src/termflow_control_plane/api/bridge.py`: reject retired/raced credentials and avoid a leaked live-registry entry.
- `apps/control-plane/tests/test_registry.py`, `test_repositories.py`, `test_terms_api.py`, `test_bridge_websocket.py`: retirement, hard delete, token revocation, audit retention, same-UUID registration, and race coverage.

### Node

- `apps/node/src/termflow_node/instances/models.py`: schema-v3 `RemoteAccessState`.
- `apps/node/src/termflow_node/instances/store.py`: atomic v3 serialization with v1/v2 compatibility.
- `apps/node/src/termflow_node/instances/manager.py`: exact Bridge start/stop, tmux validation, and attach gating.
- `apps/node/src/termflow_node/instances/activation.py` (new): explicit reactivation and local rollback.
- `apps/node/src/termflow_node/bridge/transport.py`: definitive auth-rejection classification and retry termination.
- `apps/node/src/termflow_node/tmux/actions.py`: schema-v3 rename persistence.
- `apps/node/src/termflow_node/diagnostics.py`: prevent `doctor --repair` from bypassing activation.
- `apps/node/src/termflow_node/cli.py`: `activate` command and honest local status wording.
- Node tests: `test_instance_store.py`, `test_instance_identity.py`, `test_bridge_transport.py`, `test_activation.py` (new), `test_cli_lifecycle.py`, `test_diagnostics.py`, `test_privacy.py`.

### Shared clients and Web C

- `packages/client-core/src/api/terms.ts`: transport-neutral `remove`.
- `packages/client-ui/src/components/dashboard/DeleteTermDialog.vue` (new): confirmation, focus, pending, and error presentation.
- `TermRow.vue`, `ComputerCard.vue`, `DashboardView.vue`: offline trash intent and dashboard-owned request/refresh state.
- `TerminalTitlebar.vue`, `TerminalCanvas.vue`, `useTerminalTouchGestures.ts`: cross-device viewport-lock behavior.
- `packages/client-ui/src/composables/useTerminalPageLock.ts` (new), `App.vue`, `MobileKeyBar.vue`, shared CSS: terminal-route root lock and horizontal-only key row.
- `scripts/web_e2e_fixture.py`, `scripts/run-web-e2e.sh`, `apps/clients/web/e2e/control-center.spec.ts`: disposable offline Terms and desktop/mobile acceptance.
- `apps/clients/web/e2e/deployed-smoke.spec.ts` (new): non-destructive smoke against the deployed Docker UI.

`packages/client-contracts/src/generated.ts` should remain byte-identical: DELETE returns 204 and uses the existing generated `ErrorEnvelope`. Task 9 regenerates and verifies this instead of inventing a response type.

## Phase A: Control Plane deletion and credential invalidation

### Task 1: Add the live-registry retirement gate

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/connections/registry.py:20-179`
- Test: `apps/control-plane/tests/test_registry.py`

- [ ] **Step 1: Write failing retirement tests**

```python
@pytest.mark.asyncio
async def test_retirement_blocks_late_reconnect_until_reactivated() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    instance_id = uuid4()
    connection = await registry.register(instance_id)
    with pytest.raises(InstanceOnline):
        await registry.begin_retirement(instance_id)
    await registry.unregister(connection)

    await registry.begin_retirement(instance_id)
    with pytest.raises(InstanceRetired):
        await registry.register(instance_id)

    await registry.reactivate(instance_id)
    assert (await registry.register(instance_id)).instance_id == instance_id


@pytest.mark.asyncio
async def test_failed_delete_can_cancel_retirement() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    instance_id = uuid4()
    await registry.begin_retirement(instance_id)
    await registry.cancel_retirement(instance_id)
    assert (await registry.register(instance_id)).instance_id == instance_id
```

- [ ] **Step 2: Verify the red state**

Run:

```bash
uv run --all-packages pytest apps/control-plane/tests/test_registry.py -q
```

Expected: FAIL because the retirement exceptions and methods do not exist.

- [ ] **Step 3: Implement the registry state machine**

```python
class InstanceOnline(RuntimeError):
    pass


class InstanceRetired(LookupError):
    pass


class LiveInstanceRegistry:
    def __init__(self, *, queue_size: int, queue_max_bytes: int = 1024 * 1024) -> None:
        self._queue_size = queue_size
        self._queue_max_bytes = queue_max_bytes
        self._connections: dict[UUID, LiveConnection] = {}
        self._retired: set[UUID] = set()
        self._lock = asyncio.Lock()

    async def register(self, instance_id: UUID) -> LiveConnection:
        connection = LiveConnection(
            instance_id=instance_id,
            outbound=BoundedWireQueue(
                max_messages=self._queue_size,
                max_bytes=self._queue_max_bytes,
            ),
        )
        async with self._lock:
            if instance_id in self._retired:
                raise InstanceRetired(str(instance_id))
            previous = self._connections.get(instance_id)
            self._connections[instance_id] = connection
            if previous is not None:
                previous.replaced.set()
        return connection

    async def begin_retirement(self, instance_id: UUID) -> None:
        async with self._lock:
            if instance_id in self._connections:
                raise InstanceOnline(str(instance_id))
            if instance_id in self._retired:
                raise InstanceRetired(str(instance_id))
            self._retired.add(instance_id)

    async def cancel_retirement(self, instance_id: UUID) -> None:
        async with self._lock:
            self._retired.discard(instance_id)

    async def reactivate(self, instance_id: UUID) -> None:
        async with self._lock:
            self._retired.discard(instance_id)
```

- [ ] **Step 4: Run the registry suite**

Run:

```bash
uv run --all-packages pytest apps/control-plane/tests/test_registry.py -q
```

Expected: all registry tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/connections/registry.py apps/control-plane/tests/test_registry.py
git commit -m "feat(control-plane): gate retired term reconnects"
```

### Task 2: Add exact-ID hard deletion

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py:125-225`
- Test: `apps/control-plane/tests/test_repositories.py`

- [ ] **Step 1: Write the failing repository test**

```python
@pytest.mark.asyncio
async def test_instance_delete_is_exact_and_keeps_installation_and_audit(
    repositories: RepositoryBundle,
) -> None:
    installation = await repositories.installations.create(hash_token("owner"))
    deleted_id = uuid4()
    retained_id = uuid4()
    await repositories.instances.register_or_rotate(
        deleted_id, installation.id, "deleted", hash_token("deleted-token")
    )
    await repositories.instances.register_or_rotate(
        retained_id, installation.id, "retained", hash_token("retained-token")
    )
    await repositories.audit.record(
        "term.before-delete", deleted_id, None, None, "ok", None
    )

    assert await repositories.instances.delete(deleted_id) is True
    assert await repositories.instances.delete(deleted_id) is False
    assert await repositories.instances.get(deleted_id) is None
    assert await repositories.instances.get(retained_id) is not None
    assert await repositories.installations.get(installation.id) is not None
    assert (await repositories.audit.list_all())[0].instance_id == deleted_id
```

- [ ] **Step 2: Confirm failure**

Run:

```bash
uv run --all-packages pytest apps/control-plane/tests/test_repositories.py::test_instance_delete_is_exact_and_keeps_installation_and_audit -q
```

Expected: FAIL because `InstanceRepository.delete` is missing.

- [ ] **Step 3: Implement minimal deletion**

```python
async def delete(self, instance_id: UUID) -> bool:
    async with self._sessions() as session:
        instance = await session.get(Instance, instance_id)
        if instance is None:
            return False
        await session.delete(instance)
        await session.commit()
        return True
```

- [ ] **Step 4: Run repository tests**

```bash
uv run --all-packages pytest apps/control-plane/tests/test_repositories.py -q
```

Expected: PASS; Installation and metadata-only audit rows survive.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/persistence/repositories.py apps/control-plane/tests/test_repositories.py
git commit -m "feat(control-plane): hard delete instance registration"
```

### Task 3: Expose DELETE and close the reconnect race

Public contract: `DELETE /api/v1/terms/{instance_id}` returns 204 for an offline Term, 409 `instance_online` for an online Term, and 404 `instance_not_found` for an unknown or already-retiring UUID. A successful hard delete makes the old Term token unusable while leaving the owning Computer and audit history intact.

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/api/terms.py:3-43`
- Modify: `apps/control-plane/src/termflow_control_plane/api/instances.py:17-63`
- Modify: `apps/control-plane/src/termflow_control_plane/api/bridge.py:158-208`
- Test: `apps/control-plane/tests/test_terms_api.py`
- Test: `apps/control-plane/tests/test_bridge_websocket.py`

- [ ] **Step 1: Write failing lifecycle tests**

Extend `_provision_term` to return `(instance_id, instance_token, installation_token)`, then add:

```python
def test_offline_delete_revokes_token_keeps_computer_and_allows_fresh_registration(
    client, admin_headers
) -> None:
    instance_id, old_token, installation_token = _provision_term(client, admin_headers)

    response = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
    assert response.status_code == 204
    assert client.portal.call(
        client.app.state.repositories.instances.get, instance_id
    ) is None

    dashboard = client.get("/api/v1/dashboard", headers=admin_headers).json()
    assert dashboard["metrics"]["total_terms"] == 0
    assert dashboard["computers"][0]["terms"] == []
    audits = client.portal.call(client.app.state.repositories.audit.list_all)
    assert audits[-1].operation == "term.delete"
    assert audits[-1].instance_id == instance_id

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {old_token}"},
        ):
            pass
    assert rejected.value.code == 4401

    replacement = client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {installation_token}"},
        json={"instance_id": str(instance_id), "name": "before"},
    )
    assert replacement.status_code == 201
    assert replacement.json()["instance_token"] != old_token


def test_online_and_unknown_terms_are_not_deleted(client, admin_headers) -> None:
    unknown = client.delete(f"/api/v1/terms/{uuid4()}", headers=admin_headers)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "instance_not_found"

    instance_id, token, _ = _provision_term(client, admin_headers)
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {token}"},
    ):
        online = client.delete(f"/api/v1/terms/{instance_id}", headers=admin_headers)
        assert online.status_code == 409
        assert online.json()["error"]["code"] == "instance_online"
    assert client.portal.call(
        client.app.state.repositories.instances.get, instance_id
    ) is not None
```

Add a race-boundary Bridge test:

```python
def test_retired_bridge_is_rejected_before_live_publish(client, admin_headers) -> None:
    installation_token = _installation_token(client, admin_headers)
    instance_id = uuid4()
    token = _register(client, installation_token, instance_id)
    client.portal.call(client.app.state.registry.begin_retirement, instance_id)

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/bridge/connect",
            headers={"Authorization": f"Bearer {token}"},
        ):
            pass
    assert caught.value.code == 4401
    assert client.portal.call(
        client.app.state.registry.maybe_get, instance_id
    ) is None
```

- [ ] **Step 2: Verify failure**

```bash
uv run --all-packages pytest apps/control-plane/tests/test_terms_api.py apps/control-plane/tests/test_bridge_websocket.py -q
```

Expected: FAIL because DELETE is 405 and retired Bridge registration is not handled.

- [ ] **Step 3: Implement DELETE**

```python
@router.delete(
    "/{instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_term(
    instance_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> Response:
    if await repositories.instances.get(instance_id) is None:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")
    try:
        await registry.begin_retirement(instance_id)
    except InstanceOnline as exc:
        raise TermFlowError("instance_online", 409, "The Term is online.") from exc
    except InstanceRetired as exc:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.") from exc

    try:
        deleted = await repositories.instances.delete(instance_id)
    except BaseException:
        await registry.cancel_retirement(instance_id)
        raise
    if not deleted:
        await registry.cancel_retirement(instance_id)
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")

    await repositories.audit.record("term.delete", instance_id, None, None, "ok", None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Complete registration and Bridge race handling**

Inject `LiveInstanceRegistry` into `register_instance` and call this only after `register_or_rotate` commits:

```python
await registry.reactivate(instance.id)
return InstanceRegisterResponse(instance_id=instance.id, instance_token=raw_token)
```

Before Bridge accept/presence publication:

```python
try:
    connection = await registry.register(instance.id)
except InstanceRetired:
    await websocket.close(code=4401, reason="Authentication required")
    return

confirmed = await repositories.instances.get_by_token_hash(hash_token(token))
if confirmed is None or confirmed.id != instance.id:
    await registry.unregister(connection)
    await websocket.close(code=4401, reason="Authentication required")
    return
await websocket.accept()
```

Keep connection cleanup in `finally`, including accept failure. Never log raw credentials.

- [ ] **Step 5: Run focused Control Plane coverage**

```bash
uv run --all-packages pytest apps/control-plane/tests/test_registry.py apps/control-plane/tests/test_repositories.py apps/control-plane/tests/test_terms_api.py apps/control-plane/tests/test_bridge_websocket.py apps/control-plane/tests/test_dashboard_api.py -q
```

Expected: PASS; 204/409/404 contracts, token rejection, audit retention, empty Computer, same UUID, and new token are proven.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/api/terms.py apps/control-plane/src/termflow_control_plane/api/instances.py apps/control-plane/src/termflow_control_plane/api/bridge.py apps/control-plane/tests/test_terms_api.py apps/control-plane/tests/test_bridge_websocket.py
git commit -m "feat(control-plane): delete offline term registrations"
```

## Phase B: Node state and explicit activation

### Task 4: Persist schema-v3 remote access and honest status

**Files:**
- Modify: `apps/node/src/termflow_node/instances/models.py:1-41`
- Modify: `apps/node/src/termflow_node/instances/store.py:45-70`
- Modify: `apps/node/src/termflow_node/instances/manager.py:101-160`
- Modify: `apps/node/src/termflow_node/tmux/actions.py:111-126`
- Modify: `apps/node/src/termflow_node/cli.py:61-83`
- Test: `apps/node/tests/test_instance_store.py`
- Test: `apps/node/tests/test_instance_identity.py`
- Test: `apps/node/tests/test_cli_lifecycle.py`

- [ ] **Step 1: Write failing migration/status tests**

```python
def test_v2_loads_active_and_next_save_writes_v3(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    directory = store.instance_dir(instance_id)
    directory.mkdir(parents=True, mode=0o700)
    path = store.metadata_path(instance_id)
    path.write_text(json.dumps({
        "schema_version": 2,
        "instance_id": str(instance_id),
        "name": "legacy-v2",
        "session_id": "$7",
        "session_name": "legacy-v2",
        "socket_path": str(directory / "tmux.sock"),
        "created_at": datetime.now(UTC).isoformat(),
        "bridge_pid": None,
        "instance_token": None,
        "lifecycle": "running",
    }))
    path.chmod(0o600)

    record = store.load(instance_id)
    assert record.remote_access is RemoteAccessState.ACTIVE
    store.save(record)
    dumped = json.loads(path.read_text())
    assert dumped["schema_version"] == 3
    assert dumped["remote_access"] == "active"
```

Update list/status tests to require `remote_access=active` and `bridge-running`, never `connected`. Add an activation-required record and expect `activation-required`.

- [ ] **Step 2: Confirm red**

```bash
uv run --all-packages pytest apps/node/tests/test_instance_store.py apps/node/tests/test_instance_identity.py apps/node/tests/test_cli_lifecycle.py -q
```

Expected: FAIL because schema 3 and `remote_access` are unknown.

- [ ] **Step 3: Add the v3 model and serialization**

```python
class RemoteAccessState(StrEnum):
    ACTIVE = "active"
    ACTIVATION_REQUIRED = "activation_required"


class LocalInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2, 3] = 1
    instance_id: UUID
    name: str
    session_id: str | None = None
    session_name: str = "main"
    socket_path: Path
    created_at: datetime
    bridge_pid: int | None = None
    instance_token: SecretStr | None = None
    lifecycle: InstanceLifecycle
    remote_access: RemoteAccessState = RemoteAccessState.ACTIVE

    @model_validator(mode="after")
    def stable_identity_matches_schema(self) -> "LocalInstance":
        if self.session_id is not None and not (
            self.session_id.startswith("$") and self.session_id[1:].isdigit()
        ):
            raise ValueError("session_id must be a stable tmux Session ID")
        if self.schema_version in {2, 3} and self.session_id is None:
            raise ValueError(f"schema version {self.schema_version} requires session_id")
        return self
```

In `InstanceStore.save`:

```python
serialized_version = 3 if instance.session_id is not None else instance.schema_version
payload = json.dumps(
    {
        "schema_version": serialized_version,
        "instance_id": str(instance.instance_id),
        "name": instance.name,
        "session_id": instance.session_id,
        "session_name": instance.session_name,
        "socket_path": str(instance.socket_path),
        "created_at": instance.created_at.isoformat(),
        "bridge_pid": instance.bridge_pid,
        "instance_token": token,
        "lifecycle": instance.lifecycle,
        "remote_access": instance.remote_access.value,
    },
    separators=(",", ":"),
).encode("utf-8")
```

Change stable-identity writes in `InstanceManager.create`, `InstanceManager.current`, and `TermRenamer.rename` from version 2 to 3.

- [ ] **Step 4: Change status wording**

```python
def _status_payload(record: LocalInstance) -> dict[str, object]:
    tmux_alive, bridge_alive = probe_instance_health(record)
    return {
        "instance_id": str(record.instance_id),
        "name": record.name,
        "lifecycle": record.lifecycle,
        "remote_access": record.remote_access,
        "tmux_alive": tmux_alive,
        "bridge_alive": bridge_alive,
        "socket_path": str(record.socket_path),
    }


def _status_line(payload: dict[str, object]) -> str:
    if not payload["tmux_alive"]:
        health = "tmux-down"
    elif payload["remote_access"] == RemoteAccessState.ACTIVATION_REQUIRED:
        health = "activation-required"
    elif payload["bridge_alive"]:
        health = "bridge-running"
    else:
        health = "bridge-down"
    return (
        f"{payload['instance_id']} {payload['name']} "
        f"{payload['lifecycle']} {health} "
        f"remote_access={payload['remote_access']}"
    )
```

- [ ] **Step 5: Verify Node state/status**

```bash
uv run --all-packages pytest apps/node/tests/test_instance_store.py apps/node/tests/test_instance_identity.py apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_tmux_actions.py -q
```

Expected: PASS; v1/v2 read as active, stable-ID save writes v3, and PID health is never called connected.

- [ ] **Step 6: Commit**

```bash
git add apps/node/src/termflow_node/instances/models.py apps/node/src/termflow_node/instances/store.py apps/node/src/termflow_node/instances/manager.py apps/node/src/termflow_node/tmux/actions.py apps/node/src/termflow_node/cli.py apps/node/tests/test_instance_store.py apps/node/tests/test_instance_identity.py apps/node/tests/test_cli_lifecycle.py
git commit -m "feat(node): persist remote access state"
```

### Task 5: Stop retries on definitive credential rejection

**Files:**
- Modify: `apps/node/src/termflow_node/bridge/transport.py:1-179`
- Test: `apps/node/tests/test_bridge_transport.py`

- [ ] **Step 1: Write failing definitive/transient tests**

```python
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response


@pytest.mark.asyncio
async def test_auth_rejection_requires_activation_and_stops_retry(tmp_path) -> None:
    instance = _instance(tmp_path, token="deleted-token").model_copy(
        update={"schema_version": 3, "session_id": "$0", "bridge_pid": 4321}
    )
    store = InstanceStore(tmp_path / "instances")
    store.save(instance)
    sleeps: list[float] = []

    @asynccontextmanager
    async def rejected_connect(uri: str, **kwargs):
        raise InvalidStatus(Response(403, "Forbidden", Headers()))
        yield

    transport = BridgeTransport(
        installation=_installation(),
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=lambda: TopologySnapshot(
            session_id="$0", session_name="main", revision=1, windows=[]
        ),
        connect=rejected_connect,
        sleep=lambda delay: sleeps.append(delay),
    )
    await transport.run(lambda message: asyncio.sleep(0), asyncio.Event())

    saved = store.load(instance.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    assert saved.instance_token is None
    assert saved.bridge_pid is None
    assert sleeps == []
```

Add a transient `OSError` test whose injected sleep sets shutdown; assert one backoff and active state. Add an activation-required test that registration is never called.

- [ ] **Step 2: Confirm red**

```bash
uv run --all-packages pytest apps/node/tests/test_bridge_transport.py -q
```

Expected: FAIL because every exception currently retries.

- [ ] **Step 3: Implement classification and transition**

```python
from websockets.exceptions import ConnectionClosed, InvalidStatus


def is_instance_credential_rejection(error: BaseException) -> bool:
    if isinstance(error, InvalidStatus):
        return error.response.status_code in {401, 403}
    return isinstance(error, ConnectionClosed) and error.code == 4401


def _mark_activation_required(self) -> None:
    self._instance = self._instance.model_copy(
        update={
            "schema_version": 3,
            "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
            "instance_token": None,
            "bridge_pid": None,
        }
    )
    self._store.save(self._instance)
```

At `run` entry, return when state is activation-required. In `except Exception as error`, transition and return only for a definitive rejection of an already-issued token; keep DNS/TCP/TLS/5xx/ordinary close errors on backoff.

- [ ] **Step 4: Verify**

```bash
uv run --all-packages pytest apps/node/tests/test_bridge_transport.py apps/node/tests/test_bridge_runtime.py -q
```

Expected: PASS; auth stops once, transient errors retry, shutdown remains clean.

- [ ] **Step 5: Commit**

```bash
git add apps/node/src/termflow_node/bridge/transport.py apps/node/tests/test_bridge_transport.py
git commit -m "feat(node): require activation after bridge auth rejection"
```

### Task 6: Prevent attach and doctor from bypassing activation

**Files:**
- Modify: `apps/node/src/termflow_node/instances/manager.py:195-249`
- Modify: `apps/node/src/termflow_node/diagnostics.py:83-105`
- Test: `apps/node/tests/test_instance_identity.py`
- Test: `apps/node/tests/test_diagnostics.py`

- [ ] **Step 1: Write failing gates**

```python
def test_attach_keeps_tmux_but_does_not_launch_required_bridge(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    record = _schema_v3_record(
        store,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
        bridge_pid=None,
    )
    fake = FakeRunner(record.socket_path, session_name=record.name)
    launcher = Mock(return_value=999)
    manager = InstanceManager(
        store, bridge_launcher=launcher, runner_factory=lambda path: fake
    )

    attached, argv = manager.attach(str(record.instance_id))
    assert argv[-1] == record.session_id
    assert attached.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    launcher.assert_not_called()
```

Extend diagnostics with running tmux plus activation-required, `repair=True`, and assert no launch and detail includes `activation_required`.

- [ ] **Step 2: Confirm red**

```bash
uv run --all-packages pytest apps/node/tests/test_instance_identity.py apps/node/tests/test_diagnostics.py -q
```

Expected: FAIL because attach/doctor relaunch any missing Bridge.

- [ ] **Step 3: Extract exact Bridge helpers**

```python
def require_running_tmux(self, record: LocalInstance) -> None:
    target = record.session_id
    if target is None or not self._runner_factory(record.socket_path).is_alive(target):
        raise InstanceResolutionError(
            f"Instance {record.instance_id} tmux server is not running"
        )


def stop_bridge(self, record: LocalInstance) -> LocalInstance:
    pid = record.bridge_pid
    if pid is not None and self._is_expected_bridge(pid, record.instance_id):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._is_expected_bridge(
            pid, record.instance_id
        ):
            time.sleep(0.05)
        if self._is_expected_bridge(pid, record.instance_id):
            raise BridgeStartError(
                f"Bridge process for Instance {record.instance_id} did not stop"
            )
    stopped = record.model_copy(update={"bridge_pid": None})
    self._store.save(stopped)
    return stopped


def start_bridge(self, record: LocalInstance) -> LocalInstance:
    if record.remote_access is RemoteAccessState.ACTIVATION_REQUIRED:
        return record
    if self.bridge_is_alive(record):
        return record
    started = record.model_copy(update={"bridge_pid": self._bridge_launcher(record)})
    self._store.save(started)
    return started
```

Use `require_running_tmux` in attach. Call `start_bridge` only for active state. Reuse `stop_bridge` in kill before destroying tmux.

- [ ] **Step 4: Gate doctor repair**

```python
if (
    repair
    and tmux_alive
    and not bridge_alive
    and record.remote_access is RemoteAccessState.ACTIVE
):
    try:
        pid = launch_bridge(
            record,
            log_path=instance_store.instance_dir(record.instance_id) / "bridge.log",
        )
        record = record.model_copy(update={"bridge_pid": pid})
        instance_store.save(record)
        bridge_alive = True
    except RuntimeError:
        bridge_alive = False
```

Include `remote_access=<value>` in diagnostic detail.

- [ ] **Step 5: Verify**

```bash
uv run --all-packages pytest apps/node/tests/test_instance_identity.py apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_diagnostics.py -q
```

Expected: PASS; attach returns local argv without Bridge, doctor cannot reactivate.

- [ ] **Step 6: Commit**

```bash
git add apps/node/src/termflow_node/instances/manager.py apps/node/src/termflow_node/diagnostics.py apps/node/tests/test_instance_identity.py apps/node/tests/test_diagnostics.py
git commit -m "fix(node): block implicit remote reactivation"
```

### Task 7: Implement the explicit activation transaction

**Files:**
- Create: `apps/node/src/termflow_node/instances/activation.py`
- Create: `apps/node/tests/test_activation.py`

- [ ] **Step 1: Write failing service tests**

Cover success with same UUID/fresh token/Bridge PID, active no-op, ambiguous name, missing login, dead tmux, registration failure, stale exact Bridge stop, and launch failure rollback:

```python
@pytest.mark.asyncio
async def test_activate_registers_same_uuid_and_starts_fresh_bridge(tmp_path) -> None:
    required = required_record(tmp_path)
    registered = required.model_copy(
        update={"instance_token": SecretStr("fresh-token")}
    )
    started = registered.model_copy(
        update={"remote_access": RemoteAccessState.ACTIVE, "bridge_pid": 9876}
    )
    manager = FakeManager(required, started=started)
    client = FakeRegistrationClient(registered)
    store = InstanceStore(tmp_path / "instances")
    store.save(required)

    result = await InstanceActivator(
        config_store=FakeConfigStore(_installation()),
        instance_store=store,
        manager=manager,
        control_plane=client,
    ).activate(str(required.instance_id))

    assert result.activated is True
    assert result.instance.instance_id == required.instance_id
    assert result.instance.remote_access is RemoteAccessState.ACTIVE
    assert result.instance.bridge_pid == 9876
    assert client.registered_ids == [required.instance_id]
```

On launch failure assert persisted activation-required, null token/PID, nonzero error path, and no secret in message.

- [ ] **Step 2: Confirm module is absent**

```bash
uv run --all-packages pytest apps/node/tests/test_activation.py -q
```

Expected: collection FAIL because the activation module does not exist.

- [ ] **Step 3: Implement service and rollback**

```python
@dataclass(frozen=True, slots=True)
class ActivationResult:
    instance: LocalInstance
    activated: bool


class ActivationError(RuntimeError):
    pass


class InstanceActivator:
    def __init__(
        self,
        *,
        config_store: ConfigStore,
        instance_store: InstanceStore,
        manager: InstanceManager,
        control_plane: ControlPlaneClient,
    ) -> None:
        self._config_store = config_store
        self._instance_store = instance_store
        self._manager = manager
        self._control_plane = control_plane

    async def activate(self, identifier: str) -> ActivationResult:
        try:
            record = self._manager.resolve(identifier)
        except InstanceResolutionError as exc:
            raise ActivationError(str(exc)) from exc
        if record.remote_access is RemoteAccessState.ACTIVE:
            return ActivationResult(record, False)

        try:
            installation = self._config_store.load()
            self._manager.require_running_tmux(record)
            required = self._manager.stop_bridge(record).model_copy(
                update={
                    "schema_version": 3,
                    "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
                    "instance_token": None,
                    "bridge_pid": None,
                }
            )
            self._instance_store.save(required)
            registered = await self._control_plane.register_instance(
                installation, required, self._instance_store
            )
            active = registered.model_copy(
                update={
                    "schema_version": 3,
                    "remote_access": RemoteAccessState.ACTIVE,
                }
            )
            self._instance_store.save(active)
            try:
                started = self._manager.start_bridge(active)
            except Exception as exc:
                rollback = active.model_copy(
                    update={
                        "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
                        "instance_token": None,
                        "bridge_pid": None,
                    }
                )
                self._instance_store.save(rollback)
                raise ActivationError(
                    "Bridge failed to start after registration."
                ) from exc
            return ActivationResult(started, True)
        except ActivationError:
            raise
        except Exception as exc:
            raise ActivationError(
                "Remote activation failed; local tmux was not changed."
            ) from exc
```

Fixed public errors must not echo httpx bodies, server messages, or tokens. Registration may create an offline server row before a later Bridge launch failure; retry rotates it safely.

- [ ] **Step 4: Verify activation and privacy**

```bash
uv run --all-packages pytest apps/node/tests/test_activation.py apps/node/tests/test_privacy.py -q
```

Expected: PASS for all success/error branches and secret-safety.

- [ ] **Step 5: Commit**

```bash
git add apps/node/src/termflow_node/instances/activation.py apps/node/tests/test_activation.py
git commit -m "feat(node): add explicit term activation transaction"
```

### Task 8: Expose `termflow activate`

**Files:**
- Modify: `apps/node/src/termflow_node/cli.py:86-159`
- Modify: `apps/node/tests/test_cli_lifecycle.py`
- Modify: `apps/node/tests/test_privacy.py`

- [ ] **Step 1: Write failing CLI tests**

```python
def test_activate_command_reports_success_without_credentials(
    tmp_path, monkeypatch
) -> None:
    result_record = _record(tmp_path, "alpha").model_copy(
        update={"remote_access": RemoteAccessState.ACTIVE}
    )

    async def fake_activate(self, identifier: str) -> ActivationResult:
        assert identifier == "alpha"
        return ActivationResult(result_record, True)

    monkeypatch.setattr(InstanceActivator, "activate", fake_activate)
    result = CliRunner().invoke(cli.app, ["activate", "alpha"])
    assert result.exit_code == 0
    assert f"Activated {result_record.instance_id}" in result.stdout
    assert "token" not in result.stdout.lower()
```

Add active no-op (exit 0, no rotation) and safe failure (exit 1) cases.

- [ ] **Step 2: Confirm red**

```bash
uv run --all-packages pytest apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_privacy.py -q
```

Expected: FAIL because `activate` is not a command.

- [ ] **Step 3: Implement command**

```python
@app.command()
def activate(identifier: str) -> None:
    """Explicitly restore remote access for one locally running Term."""

    store = InstanceStore.default()
    activator = InstanceActivator(
        config_store=ConfigStore.default(),
        instance_store=store,
        manager=InstanceManager(store),
        control_plane=ControlPlaneClient(),
    )
    try:
        result = asyncio.run(activator.activate(identifier))
    except ActivationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if result.activated:
        typer.echo(f"Activated {result.instance.instance_id}")
    else:
        typer.echo(
            f"Remote access already active for {result.instance.instance_id}"
        )
```

Do not echo raw dependency exceptions. Preserve safe ambiguous candidate UUIDs.

- [ ] **Step 4: Run Node unit suite**

```bash
uv run --all-packages pytest apps/node/tests -q -m "not tmux and not e2e"
```

Expected: PASS; attach remains local and activate is the only recovery path.

- [ ] **Step 5: Commit**

```bash
git add apps/node/src/termflow_node/cli.py apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_privacy.py
git commit -m "feat(node): expose explicit termflow activate command"
```

## Phase C: Shared deletion UI

### Task 9: Add transport-neutral `terms.remove`

**Files:**
- Modify: `packages/client-core/src/api/terms.ts:1-17`
- Modify: `packages/client-core/src/http/apiClient.test.ts:61-84`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts:13-16`

- [ ] **Step 1: Write failing request tests**

```typescript
it('removes a Term through DELETE and accepts 204', async () => {
  const request = vi.fn().mockResolvedValue(response(204, undefined, ''))
  await expect(createApiClient({ request }).terms.remove('term /2')).resolves.toBeUndefined()
  expect(request).toHaveBeenCalledWith(
    '/api/v1/terms/term%20%2F2',
    { method: 'DELETE' },
  )
})
```

Also include this call in the fixed-public-path matrix.

- [ ] **Step 2: Confirm red**

```bash
npm run test:run --workspace @termflow/client-core -- src/http/apiClient.test.ts
```

Expected: FAIL because `remove` is missing.

- [ ] **Step 3: Implement API and fake runtime**

```typescript
remove: (id: string, signal?: AbortSignal) => request<void>(
  `/api/v1/terms/${encodeURIComponent(id)}`,
  withSignal({ method: 'DELETE' }, signal),
),
```

Add `remove: async () => undefined` to the fake runtime's `terms`.

- [ ] **Step 4: Regenerate/check contracts and test**

```bash
npm run contracts:generate
git diff --exit-code -- packages/client-contracts/src/generated.ts
npm run contracts:check
npm run test:run --workspace @termflow/client-core
```

Expected: generated contract unchanged and all commands PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/client-core/src/api/terms.ts packages/client-core/src/http/apiClient.test.ts packages/client-ui/src/test/fakeRuntime.ts
git commit -m "feat(client): add offline term removal request"
```

### Task 10: Add the trash SVG and accessible confirmation dialog

**Files:**
- Create: `packages/client-ui/src/components/dashboard/DeleteTermDialog.vue`
- Create: `packages/client-ui/src/components/dashboard/DeleteTermDialog.test.ts`
- Modify: `packages/client-ui/src/components/dashboard/TermRow.vue:1-23`
- Modify: `packages/client-ui/src/components/dashboard/ComputerCard.vue:1-19`
- Modify: `packages/client-ui/src/styles/app.css:60-70,83-89,138-139`

- [ ] **Step 1: Write failing component tests**

```typescript
it('explains the remote/local boundary and restores focus', async () => {
  const invoker = document.createElement('button')
  document.body.append(invoker)
  invoker.focus()
  const wrapper = mount(DeleteTermDialog, {
    attachTo: document.body,
    props: { term: offlineTerm, pending: false, error: '' },
  })
  await wrapper.vm.$nextTick()

  expect(wrapper.get('[role="alertdialog"]').text())
    .toContain('不会删除本地 tmux Session')
  expect(wrapper.text())
    .toContain(`termflow activate ${offlineTerm.instance_id}`)
  expect(document.activeElement)
    .toBe(wrapper.get('[data-action="cancel-delete-term"]').element)
  await wrapper.get('[data-action="confirm-delete-term"]').trigger('click')
  expect(wrapper.emitted('confirm')).toEqual([[offlineTerm.instance_id]])
  wrapper.unmount()
  expect(document.activeElement).toBe(invoker)
})
```

Mount online/offline `TermRow`; only the offline ARTICLE may contain one Lucide button named `删除离线 Term：离线维护`.

- [ ] **Step 2: Confirm red**

```bash
npm run test:run --workspace @termflow/client-ui -- src/components/dashboard/DeleteTermDialog.test.ts src/views/DashboardView.test.ts
```

Expected: FAIL because the dialog and trash event do not exist.

- [ ] **Step 3: Implement the dialog contract**

```typescript
const props = defineProps<{
  term: TermSummary
  pending: boolean
  error: string
}>()
defineEmits<{
  confirm: [instanceId: string]
  cancel: []
}>()
```

The confirm control:

```vue
<button
  data-action="confirm-delete-term"
  class="danger-button"
  type="button"
  :disabled="pending"
  @click="$emit('confirm', term.instance_id)"
>
  {{ pending ? '正在删除…' : '永久删除远程 Term' }}
</button>
```

Use `role="alertdialog"`, `aria-modal`, labelled/described IDs, initial cancel focus, Tab wrap, Escape/backdrop cancel only when not pending, error `role="alert"`, and connected-invoker focus restoration.

- [ ] **Step 4: Add offline-only Trash2 event**

```vue
<button
  v-if="!term.online"
  data-action="delete-offline-term"
  class="icon-button icon-only destructive term-delete-button"
  type="button"
  :aria-label="`删除离线 Term：${term.name}`"
  :title="`删除离线 Term：${term.name}`"
  @click="$emit('request-delete', term)"
>
  <Trash2 :size="18" aria-hidden="true" />
</button>
```

Declare/forward `request-delete` through `ComputerCard`. Add a final row grid column and destructive focus/hover color; keep online RouterLink free of nested buttons.

- [ ] **Step 5: Verify accessibility/responsive contracts**

```bash
npm run test:run --workspace @termflow/client-ui -- src/components/dashboard/DeleteTermDialog.test.ts src/test/a11y-contract.test.ts src/test/responsive-contract.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/client-ui/src/components/dashboard/DeleteTermDialog.vue packages/client-ui/src/components/dashboard/DeleteTermDialog.test.ts packages/client-ui/src/components/dashboard/TermRow.vue packages/client-ui/src/components/dashboard/ComputerCard.vue packages/client-ui/src/styles/app.css
git commit -m "feat(client): add offline term deletion dialog"
```

### Task 11: Orchestrate deletion in DashboardView

**Files:**
- Modify: `packages/client-ui/src/views/DashboardView.vue:1-25`
- Modify: `packages/client-ui/src/views/DashboardView.test.ts:1-150`

- [ ] **Step 1: Write failing success/error tests**

```typescript
it('waits for DELETE then refreshes the authoritative dashboard', async () => {
  let resolveDelete!: () => void
  const remove = vi.fn(() => new Promise<void>((resolve) => {
    resolveDelete = resolve
  }))
  const get = vi.fn()
    .mockResolvedValueOnce(dashboard)
    .mockResolvedValueOnce(withoutOfflineTerm)
  const runtime = createFakeRuntime({
    api: { dashboard: { get }, terms: { remove } } as unknown as ClientRuntime['api'],
  })
  const wrapper = await mountDashboard(runtime)
  await flushPromises()

  await wrapper.get(
    '[data-term-id="term-2"] [data-action="delete-offline-term"]',
  ).trigger('click')
  await wrapper.get('[data-action="confirm-delete-term"]').trigger('click')
  expect(wrapper.find('[data-term-id="term-2"]').exists()).toBe(true)
  expect(
    wrapper.get('[data-action="confirm-delete-term"]').attributes('disabled'),
  ).toBeDefined()

  resolveDelete()
  await flushPromises()
  expect(remove).toHaveBeenCalledWith('term-2')
  expect(get).toHaveBeenCalledTimes(2)
  expect(wrapper.find('[data-term-id="term-2"]').exists()).toBe(false)
  expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
})
```

Add:
- `new ApiError('validation', { status: 409, code: 'instance_online' })` → `Term 已重新上线，无法删除。`, dialog/row remain.
- `instance_not_found` → one authoritative refresh; no pre-refresh splice.
- generic failure → safe message and retry enabled.
- double click while pending → one DELETE call.

- [ ] **Step 2: Confirm red**

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/DashboardView.test.ts
```

Expected: FAIL because DashboardView owns no deletion state.

- [ ] **Step 3: Implement dashboard-owned state**

```typescript
const selectedForDeletion = ref<TermSummary | null>(null)
const deletePending = ref(false)
const deleteError = ref('')

function requestDelete(term: TermSummary) {
  selectedForDeletion.value = term
  deleteError.value = ''
}

function deleteMessage(error: unknown) {
  if (error instanceof ApiError && error.code === 'instance_online') {
    return 'Term 已重新上线，无法删除。'
  }
  if (error instanceof ApiError && error.code === 'instance_not_found') {
    return 'Term 已不存在；列表已按服务器状态刷新。'
  }
  return error instanceof ApiError ? error.message : '无法删除 Term，请重试。'
}

async function confirmDelete(instanceId: string) {
  if (deletePending.value) return
  deletePending.value = true
  deleteError.value = ''
  try {
    await runtime.api.terms.remove(instanceId)
    await refresh()
    selectedForDeletion.value = null
  } catch (error) {
    deleteError.value = deleteMessage(error)
    if (error instanceof ApiError && error.code === 'instance_not_found') {
      await refresh()
    }
  } finally {
    deletePending.value = false
  }
}
```

Pass `request-delete` from cards, render `DeleteTermDialog`, block cancel while pending, and never splice snapshot state.

- [ ] **Step 4: Verify shared dashboard**

```bash
npm run test:run --workspace @termflow/client-ui -- src/views/DashboardView.test.ts src/components/dashboard/DeleteTermDialog.test.ts src/test/a11y-contract.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui/src/views/DashboardView.vue packages/client-ui/src/views/DashboardView.test.ts
git commit -m "feat(client): orchestrate offline term deletion"
```

## Phase D: Viewport locking and page containment

### Task 12: Unify viewport-lock meaning

**Files:**
- Modify: `packages/client-ui/src/views/TerminalView.vue:1-114`
- Modify: `packages/client-ui/src/views/TerminalView.test.ts`
- Modify: `packages/client-ui/src/components/terminal/TerminalTitlebar.vue:1-62`
- Modify: `packages/client-ui/src/components/terminal/TerminalCanvas.vue:1-148`
- Modify: `packages/client-ui/src/components/terminal/TerminalCanvas.test.ts`
- Modify: `packages/client-ui/src/composables/useTerminalTouchGestures.ts:39-142`
- Modify: `packages/client-ui/src/composables/useTerminalTouchGestures.test.ts`
- Modify: `packages/client-ui/src/styles/app.css:101-147`
- Modify: `packages/client-ui/src/styles/terminal-responsive.css:1-18`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing semantic tests**

```typescript
it('does not pan a locked viewport while disconnected', () => {
  const { gestures, viewport, dispatchMouse, setConnected } = harness(true)
  setConnected(false)
  gestures.pointerDown(point(1, 100, 100))
  gestures.pointerMove(point(1, 40, 40))
  gestures.pointerUp(1, point(1, 40, 40))
  expect(viewport.pointerDown).not.toHaveBeenCalled()
  expect(viewport.pointerMove).not.toHaveBeenCalled()
  expect(dispatchMouse).not.toHaveBeenCalled()
})
```

In Canvas tests set native `scrollLeft=120`, `scrollTop=80`; lock/unlock and assert both remain. Assert `resetViewport()` resets both to zero. In TerminalView test, lock defaults false, toggles true, and stays true across a resize/orientation event.

- [ ] **Step 2: Confirm red**

```bash
npm run test:run --workspace @termflow/client-ui -- src/composables/useTerminalTouchGestures.test.ts src/components/terminal/TerminalCanvas.test.ts src/views/TerminalView.test.ts src/test/responsive-contract.test.ts
```

Expected: FAIL because state/selectors are touch-specific, disconnected lock pans, and active CSS is coarse-pointer-only.

- [ ] **Step 3: Rename the internal/public bindings**

Use `viewportLocked`, `v-model:viewport-locked`, prop/event `viewportLocked`/`update:viewportLocked`, action `toggle-viewport-lock`, class `viewport-lock-button`, and canvas attribute `data-viewport-lock="locked|unlocked"`. Keep `锁定画布`, `解除画布锁定`, and `aria-pressed`.

- [ ] **Step 4: Enforce locked/unlocked routing**

```typescript
function pointerDown(point: PointerSample) {
  if (!options.locked()) {
    viewportPointers.add(point.pointerId)
    options.viewport.pointerDown(point)
    return
  }
  lockedPointers.add(point.pointerId)
  if (!options.connected()) return
  if (lockedBlocked) return
  if (active) {
    finishLocked()
    lockedBlocked = true
    return
  }
  active = {
    pointerId: point.pointerId,
    start: point,
    current: point,
    phase: 'pending',
    timer: null,
  }
  active.timer = setTimeout(() => {
    if (!active || active.phase !== 'pending') return
    active.phase = 'selection'
    options.dispatchMouse(mouse('mousedown', active.start, true, 2))
  }, longPressMs)
}
```

Mouse handlers still return so xterm retains keyboard, selection, and tmux mouse. Reset both transform and native scroll:

```typescript
function resetViewport() {
  pendingFocusPane = null
  pointer.reset()
  frameElement.value?.scrollTo({ left: 0, top: 0 })
}
```

Never reset native offsets merely because lock changes.

- [ ] **Step 5: Move active/overflow CSS to shared base**

```css
.viewport-lock-button[aria-pressed='true'] {
  border-color: var(--color-accent);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
}
.terminal-frame[data-viewport-lock='locked'] { overflow: hidden; }
.terminal-frame[data-display-mode='fit'] { overflow: hidden; }
```

Remove the coarse-only active rule. Keep unlocked non-fit `overflow:auto` for desktop x/y overflow and mobile `.terminal-frame { touch-action:none; }`.

- [ ] **Step 6: Verify terminal UI/typecheck**

```bash
npm run test:run --workspace @termflow/client-ui -- src/composables/usePointerViewport.test.ts src/composables/useTerminalTouchGestures.test.ts src/components/terminal/TerminalCanvas.test.ts src/views/TerminalView.test.ts src/test/responsive-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: PASS; unlocked mobile pans/pinches, locked mobile fixes viewport and routes connected remote mouse/long-press, desktop lock preserves offsets.

- [ ] **Step 7: Commit**

```bash
git add packages/client-ui/src/views/TerminalView.vue packages/client-ui/src/views/TerminalView.test.ts packages/client-ui/src/components/terminal/TerminalTitlebar.vue packages/client-ui/src/components/terminal/TerminalCanvas.vue packages/client-ui/src/components/terminal/TerminalCanvas.test.ts packages/client-ui/src/composables/useTerminalTouchGestures.ts packages/client-ui/src/composables/useTerminalTouchGestures.test.ts packages/client-ui/src/styles/app.css packages/client-ui/src/styles/terminal-responsive.css packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(client): unify terminal viewport locking"
```

### Task 13: Lock terminal-route roots and contain the key row

**Files:**
- Create: `packages/client-ui/src/composables/useTerminalPageLock.ts`
- Create: `packages/client-ui/src/composables/useTerminalPageLock.test.ts`
- Modify: `packages/client-ui/src/App.vue:33-49`
- Modify: `packages/client-ui/src/App.test.ts`
- Modify: `packages/client-ui/src/components/terminal/MobileKeyBar.vue:1-27`
- Modify: `packages/client-ui/src/components/terminal/MobileKeyBar.test.ts`
- Modify: `packages/client-ui/src/styles/reset.css:1-10`
- Modify: `packages/client-ui/src/styles/terminal-responsive.css:1-18`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`

- [ ] **Step 1: Write failing lifecycle tests**

```typescript
it('locks html body and app only while terminal route is active', async () => {
  const root = document.createElement('div')
  root.id = 'app'
  document.body.append(root)
  const active = ref(false)
  const wrapper = mount(defineComponent({
    setup() {
      useTerminalPageLock(active)
      return () => h('div')
    },
  }), { attachTo: root })

  active.value = true
  await nextTick()
  for (const element of [document.documentElement, document.body, root]) {
    expect(element.classList.contains('termflow-terminal-route')).toBe(true)
  }
  active.value = false
  await nextTick()
  for (const element of [document.documentElement, document.body, root]) {
    expect(element.classList.contains('termflow-terminal-route')).toBe(false)
  }
  wrapper.unmount()
})
```

In App test navigate dashboard → Term → dashboard and assert add/remove. Keybar test asserts no vertical drag emits terminal input.

- [ ] **Step 2: Confirm red**

```bash
npm run test:run --workspace @termflow/client-ui -- src/composables/useTerminalPageLock.test.ts src/App.test.ts src/components/terminal/MobileKeyBar.test.ts src/test/responsive-contract.test.ts
```

Expected: FAIL because route lock and explicit key-row gesture contract are absent.

- [ ] **Step 3: Implement route lock with scroll restore**

```typescript
const CLASS_NAME = 'termflow-terminal-route'

export function useTerminalPageLock(active: Readonly<Ref<boolean>>) {
  let locked = false
  let scrollX = 0
  let scrollY = 0
  const roots = () => [
    document.documentElement,
    document.body,
    document.getElementById('app'),
  ].filter((element): element is HTMLElement => element instanceof HTMLElement)

  function unlock() {
    if (!locked) return
    roots().forEach((element) => element.classList.remove(CLASS_NAME))
    locked = false
    window.scrollTo(scrollX, scrollY)
  }

  watch(active, (enabled) => {
    if (!enabled) {
      unlock()
      return
    }
    if (locked) return
    scrollX = window.scrollX
    scrollY = window.scrollY
    roots().forEach((element) => element.classList.add(CLASS_NAME))
    locked = true
  }, { immediate: true })
  onBeforeUnmount(unlock)
}
```

Call `useTerminalPageLock(terminalLayout)` once in App.

- [ ] **Step 4: Add root/key-row CSS**

```css
html.termflow-terminal-route,
body.termflow-terminal-route,
#app.termflow-terminal-route {
  width: 100%;
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
  overscroll-behavior: none;
}
body.termflow-terminal-route { position: fixed; inset: 0; }
.mobile-keybar {
  touch-action: pan-x;
  overscroll-behavior-inline: contain;
  overscroll-behavior-block: none;
  background: var(--color-panel);
}
```

Retain horizontal overflow, static third row, bottom safe-area paint, and terminal `touch-action:none`. Do not force fit mode.

- [ ] **Step 5: Verify**

```bash
npm run test:run --workspace @termflow/client-ui -- src/composables/useTerminalPageLock.test.ts src/App.test.ts src/components/terminal/MobileKeyBar.test.ts src/test/responsive-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: PASS; dashboard scrolling returns after route exit and vertical key-row drag has no document target.

- [ ] **Step 6: Commit**

```bash
git add packages/client-ui/src/composables/useTerminalPageLock.ts packages/client-ui/src/composables/useTerminalPageLock.test.ts packages/client-ui/src/App.vue packages/client-ui/src/App.test.ts packages/client-ui/src/components/terminal/MobileKeyBar.vue packages/client-ui/src/components/terminal/MobileKeyBar.test.ts packages/client-ui/src/styles/reset.css packages/client-ui/src/styles/terminal-responsive.css packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(client): contain mobile terminal route gestures"
```

## Phase E: Browser evidence and deployment

### Task 14: Extend isolated browser acceptance

**Files:**
- Modify: `scripts/web_e2e_fixture.py:17-70`
- Modify: `scripts/run-web-e2e.sh:1-90`
- Modify: `apps/clients/web/e2e/control-center.spec.ts:1-534`
- Create: `apps/clients/web/e2e/deployed-smoke.spec.ts`

- [ ] **Step 1: Create one offline registration per Playwright project**

After the real online Term is ready:

```python
offline_ids: dict[str, str] = {}
installation = ConfigStore.default().load()
for project in ("desktop", "mobile-portrait", "mobile-landscape"):
    offline_id = uuid4()
    response = httpx.post(
        f"{base_url}/api/v1/instances/register",
        headers={
            "Authorization": "Bearer "
            + installation.installation_token.get_secret_value()
        },
        json={"instance_id": str(offline_id), "name": f"offline-{project}"},
        timeout=3,
    )
    response.raise_for_status()
    offline_ids[project] = str(offline_id)
print(json.dumps({
    "online_term_id": str(instance.instance_id),
    "offline_term_ids": offline_ids,
}))
```

Parse this JSON in `run-web-e2e.sh`, export `TERMFLOW_E2E_TERM_ID` and `TERMFLOW_E2E_OFFLINE_TERM_IDS`, and clean up only the real local tmux Term.

- [ ] **Step 2: Cover deletion UI and online conflict**

For each project, delete its offline row through the alertdialog, verify local-tmux warning and UUID activation command, wait for 204, reload, and assert absence. Assert the online row has no trash action. Send a bearer-authenticated DELETE for the online UUID and assert 409 `instance_online`.

- [ ] **Step 3: Cover desktop 100% x/y locking**

Turn tmux mouse off temporarily, select 100%, assert both scroll axes overflow, use trusted wheel `deltaX` and `deltaY`, and assert both offsets grow. Lock, wheel again, and assert offsets unchanged. Unlock and prove movement resumes. Re-enable tmux mouse before existing remote wheel/selection checks. Fit mode must reset offsets and fit both axes.

- [ ] **Step 4: Cover mobile pan/pinch/lock/keybar containment**

For portrait and landscape:
- 100% has horizontal and vertical overflow.
- separate one-finger horizontal and vertical pans change transform;
- pinch changes visual scale;
- locked pan/pinch preserves transform, scale, and frame scroll;
- locked tap/drag and long press still prove remote mouse/local selection;
- vertical keybar drag preserves `window.scrollY`, html/body scrollTop, visual viewport offset, and title/frame/keybar rectangles;
- horizontal keybar drag changes only keybar `scrollLeft`;
- keybar bottom stays inside visual viewport and no background is exposed.

- [ ] **Step 5: Add deployed read-only smoke**

The new spec logs in, verifies dashboard metrics/SVG controls, opens the first online Term only when present, checks default unlocked state and terminal-route root classes, navigates back, and checks cleanup. It must not rename/delete, create enrollment, send terminal bytes, or mutate tmux.

- [ ] **Step 6: Run isolated Playwright**

```bash
./scripts/run-web-e2e.sh
```

Expected: desktop, mobile portrait, and mobile landscape tests PASS; failure artifacts are retained automatically.

- [ ] **Step 7: Commit**

```bash
git add scripts/web_e2e_fixture.py scripts/run-web-e2e.sh apps/clients/web/e2e/control-center.spec.ts apps/clients/web/e2e/deployed-smoke.spec.ts
git commit -m "test(web): cover term deletion and viewport containment"
```

### Task 15: Full verification, Docker deployment, and deployed UI smoke

**Files:**
- Verification/deployment only; no source edits expected.

- [ ] **Step 1: Run static/build gates**

```bash
git diff --check
npm run contracts:check
npm run test:run
npm run typecheck
npm run build:web
uv run --all-packages ruff check .
uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src
```

Expected: every command exits 0.

- [ ] **Step 2: Run focused lifecycle and real-tmux integration**

```bash
uv run --all-packages pytest apps/control-plane/tests/test_registry.py apps/control-plane/tests/test_repositories.py apps/control-plane/tests/test_terms_api.py apps/control-plane/tests/test_bridge_websocket.py apps/node/tests/test_instance_store.py apps/node/tests/test_bridge_transport.py apps/node/tests/test_activation.py apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_diagnostics.py -q
uv run --all-packages pytest apps/node/tests/integration -q
```

Expected: all selected unit/integration tests PASS.

- [ ] **Step 3: Run complete repository and browser gates**

```bash
./scripts/verify.sh
./scripts/run-web-e2e.sh
```

Expected: both exit 0. Record exact test counts/durations; a partial run is not full verification.

- [ ] **Step 4: Confirm clean implementation HEAD**

```bash
git status --short --branch
git log -15 --oneline --decorate
```

Expected: clean implementation worktree. Any source correction returns to its owning red/green task before deployment.

- [ ] **Step 5: Capture current deployment credential without printing it**

```bash
TERMFLOW_DEPLOY_ADMIN_TOKEN="$(docker inspect deploy-control-plane-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^TERMFLOW_ADMIN_TOKEN=//p')"
test -n "$TERMFLOW_DEPLOY_ADMIN_TOKEN"
```

Expected: exit 0 and no token output. If the container name differs, first resolve the exact port-8765 target with `docker ps --filter publish=8765`.

- [ ] **Step 6: Build, tag the verified digest, and recreate only Control Plane**

```bash
TERMFLOW_ADMIN_TOKEN="$TERMFLOW_DEPLOY_ADMIN_TOKEN" docker compose -f deploy/compose.yaml build control-plane
docker tag deploy-control-plane:latest termflow-control-plane:local
TERMFLOW_ADMIN_TOKEN="$TERMFLOW_DEPLOY_ADMIN_TOKEN" docker compose -f deploy/compose.yaml up -d --no-deps --force-recreate control-plane
```

Expected: Compose reuses named volume `termflow-data`; it does not remove local metadata.

- [ ] **Step 7: Verify identity, health, volume, and static UI**

```bash
docker inspect deploy-control-plane-1 --format '{{.Image}} {{.State.Health.Status}}'
docker image inspect termflow-control-plane:local --format '{{.Id}} {{.Created}}'
docker volume inspect termflow-data --format '{{.Name}}'
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/ | sed -n '1,20p'
```

Expected: container and tagged image IDs match, health is `healthy`, volume is `termflow-data`, health JSON is `{"status":"ok"}`, and `/` serves built Web C.

- [ ] **Step 8: Run deployed non-destructive browser smoke**

```bash
TERMFLOW_E2E_BASE_URL=http://127.0.0.1:8765 \
TERMFLOW_E2E_ADMIN_TOKEN="$TERMFLOW_DEPLOY_ADMIN_TOKEN" \
npm run e2e --workspace @termflow/web-client -- --grep "deployed read-only smoke"
unset TERMFLOW_DEPLOY_ADMIN_TOKEN
```

Expected: desktop, portrait, and landscape smoke projects PASS without state mutation.

- [ ] **Step 9: Review and finish the branch**

```bash
git status --short --branch
git log -1 --format='%H %s'
```

Expected: clean exact HEAD. Invoke `superpowers:requesting-code-review`, address evidence-backed findings, rerun affected/full gates, then use `superpowers:finishing-a-development-branch`. The handoff must separately report implementation commit, focused/full test counts, isolated Playwright result, Docker IDs/health, deployed smoke result, and any live-unverified gap.
