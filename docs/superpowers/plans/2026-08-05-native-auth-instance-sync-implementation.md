# TermFlow Native Auth and Instance Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 Web C/Tauri 的双路径授权闭环、统一成功反馈和响应式页面，并让 A 能同步远程实例、显示连接状态、清理失效旧实例。

**Architecture:** B 新增一个仅返回当前安装所属实例的 installation-token API；A 通过显式 `sync` 保存远程状态，`prune` 只删除安全候选。Web C 复用已授权客户端面板和现有授权 API，Tauri 由 `client-core` 统一浏览器回调/设备码状态，Rust 继续负责 DPoP 和平台 keyring。共享 BottomToastHost 挂载在 UI App 中，Web C 和 Tauri 都调用同一组件。

**Tech Stack:** Python 3.12, FastAPI, Pydantic, httpx, Typer, pytest; Vue 3, TypeScript, Vitest, Vue Test Utils; Tauri 2, Rust, keyring, Playwright/真实浏览器验收。

## 当前交付状态（2026-08-06）

- 已提交：Task 1-4，分别覆盖安装范围实例查询、A 的同步/清理命令、共享底部 Toast，以及设置中的设备授权入口。
- 已实现且正在收尾审查：Task 5。原生浏览器回调和设备码流程共用授权状态；刷新令牌与 DPoP 私钥仍留在 Rust/keyring，WebView 只保留进程内 access credential。
- 已在本机验证：Web C 的 Playwright 桌面/移动流程、A/B 的“B 删除离线 Term -> A sync -> dry-run -> force prune”流程、Python/TypeScript/Rust 测试、Tauri 无安装包 release 构建，以及最新 B + Web C Docker 镜像。
- 尚未完成真实验收：安装最新 Windows 包后的浏览器回调、设备码、重启恢复和诊断日志；GitHub Actions 的 Windows/Linux/Android/iOS 构建矩阵。它们仍是 Task 6 的发布前门槛，不能由本机单测替代。

---

## 文件责任地图

- `apps/control-plane/src/termflow_control_plane/api/instances.py`：新增 installation-scoped 实例列表接口。
- `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`：按 installation 查询未撤销实例。
- `apps/node/src/termflow_node/control_plane_client.py`：调用 B 的实例同步接口和健康探测。
- `apps/node/src/termflow_node/instances/models.py`、`store.py`：保存远程状态、同步时间和错误，并兼容旧元数据。
- `apps/node/src/termflow_node/instances/synchronization.py`：同步、候选判断和本地清理策略。
- `apps/node/src/termflow_node/cli.py`、`diagnostics.py`：暴露 `sync/prune`，扩展 `list/doctor` 状态。
- `packages/client-core/src/auth/authorizationState.ts`：共享授权状态和终态类型。
- `packages/client-core/src/auth/nativeAuthorization.ts`、`deviceAuthorization.ts`：发出状态变化，确保取消和重复轮询安全。
- `packages/client-ui/src/components/common/BottomToast.vue`、`useBottomToast.ts`、`App.vue`：全局底部 Toast。
- `packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue`、新授权弹窗：设置中的设备码审批入口。
- `packages/client-ui/src/views/LoginView.vue`、`DashboardView.vue`、`DeviceAuthorizeView.vue`：移除错误入口，复用审批逻辑和统一布局。
- `apps/clients/tauri/src/views/NativeConnectView.vue`、`NativeDeviceAuthorizeView.vue`：原生登录首屏和独立设备授权页。
- `apps/clients/tauri/src/nativeAuth.ts`、`adapters/tauriAuthorization.ts`、`src-tauri/src/auth.rs`：统一状态、深链诊断、keyring 恢复和 logout 清理。

## Task 1: 增加 installation-scoped 远程实例列表 API

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py:319-420`
- Modify: `apps/control-plane/src/termflow_control_plane/api/instances.py:75-115`
- Test: `apps/control-plane/tests/test_instance_api.py`

- [ ] **Step 1: 写失败测试，证明 installation token 只能看到自己的实例**

在 `apps/control-plane/tests/test_instance_api.py` 添加：

```python
def test_installation_can_list_only_its_active_instances(client, admin_headers, provision_computer):
    owner = provision_computer(hostname="owner")
    other = provision_computer(hostname="other")
    client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {owner.installation_token}"},
        json={"instance_id": str(uuid4()), "name": "owner-term"},
    )
    client.post(
        "/api/v1/instances/register",
        headers={"Authorization": f"Bearer {other.installation_token}"},
        json={"instance_id": str(uuid4()), "name": "other-term"},
    )

    response = client.get(
        "/api/v1/instances/mine",
        headers={"Authorization": f"Bearer {owner.installation_token}"},
    )

    assert response.status_code == 200
    assert {item["name"] for item in response.json()["instances"]} == {"owner-term"}
```

- [ ] **Step 2: 运行测试确认接口不存在或认证失败**

Run: `uv run pytest apps/control-plane/tests/test_instance_api.py::test_installation_can_list_only_its_active_instances -q`

Expected: FAIL because `/api/v1/instances/mine` has not been implemented.

- [ ] **Step 3: 添加 repository 查询方法**

在 `InstanceRepository` 中加入以下方法，过滤已撤销实例并稳定排序：

```python
async def list_for_installation(self, installation_id: UUID) -> list[Instance]:
    async with self._sessions() as session:
        result = await session.scalars(
            select(Instance)
            .where(
                Instance.installation_id == installation_id,
                Instance.revoked_at.is_(None),
            )
            .order_by(Instance.created_at)
        )
        return list(result)
```

- [ ] **Step 4: 添加 `GET /api/v1/instances/mine`**

在 `/{instance_id}` 路由之前加入 installation-scoped 路由：

```python
@router.get("/mine", response_model=InstanceListResponse)
async def list_owned_instances(
    installation: Annotated[Installation, Depends(require_installation)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> InstanceListResponse:
    instances = await repositories.instances.list_for_installation(installation.id)
    online = await registry.online_ids()
    return InstanceListResponse(
        instances=[_instance_response(instance, instance.id in online) for instance in instances]
    )
```

- [ ] **Step 5: 验证并提交**

Run: `uv run pytest apps/control-plane/tests/test_instance_api.py -q`

Expected: PASS, including admin list behavior and installation isolation.

Commit: `git add apps/control-plane/src/termflow_control_plane/api/instances.py apps/control-plane/src/termflow_control_plane/persistence/repositories.py apps/control-plane/tests/test_instance_api.py && git commit -m "feat: expose installation-scoped instance listing"`

## Task 2: 实现 A 的远程状态、同步和安全清理

**Files:**
- Modify: `apps/node/src/termflow_node/instances/models.py`
- Modify: `apps/node/src/termflow_node/instances/store.py`
- Create: `apps/node/src/termflow_node/instances/synchronization.py`
- Modify: `apps/node/src/termflow_node/control_plane_client.py`
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/src/termflow_node/diagnostics.py`
- Test: `apps/node/tests/test_instance_store.py`
- Test: `apps/node/tests/test_instance_sync.py`
- Test: `apps/node/tests/test_cli_lifecycle.py`
- Test: `apps/node/tests/test_diagnostics.py`

- [ ] **Step 1: 写旧元数据兼容和远程状态失败测试**

在 `test_instance_store.py` 添加一个没有新字段的 schema 3 `metadata.json`，加载后断言默认值为 `unknown`，保存后断言写出 schema 4；在 `test_instance_sync.py` 添加：

```python
# test_instance_sync.py imports:
# from datetime import UTC, datetime
# from uuid import uuid4
# from termflow_node.config.models import InstallationConfig
# from termflow_node.instances.models import InstanceLifecycle, LocalInstance
# from termflow_protocol import InstanceListResponse, InstanceResponse

def _record(store: InstanceStore, name: str) -> LocalInstance:
    instance_id = uuid4()
    return LocalInstance(
        instance_id=instance_id,
        name=name,
        socket_path=store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        bridge_pid=None,
        instance_token="instance-token-for-test",
        lifecycle=InstanceLifecycle.STOPPED,
    )


class FakeControlPlaneClient:
    def __init__(self, remote_instances: list[InstanceResponse]) -> None:
        self.remote_instances = remote_instances

    async def list_owned_instances(self, installation: InstallationConfig) -> InstanceListResponse:
        return InstanceListResponse(instances=self.remote_instances)


async def test_sync_marks_local_records_missing_from_b(tmp_path):
    store = InstanceStore(tmp_path / "instances")
    local = _record(store, "deleted-remotely")
    store.save(local)
    client = FakeControlPlaneClient(remote_instances=[])

    result = await InstanceSynchronizer(store, client, InstallationConfig(
        server_url="https://relay.example.com",
        installation_id=uuid4(),
        installation_token="installation-token-for-test",
    )).sync()

    assert result.remote_deleted == [local.instance_id]
    assert store.load(local.instance_id).remote_status == RemoteInstanceStatus.REMOTE_DELETED
    assert store.load(local.instance_id).last_sync_error is None
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest apps/node/tests/test_instance_store.py apps/node/tests/test_instance_sync.py -q`

Expected: FAIL because `RemoteInstanceStatus`, schema 4 fields and `InstanceSynchronizer` do not exist.

- [ ] **Step 3: 扩展模型和原子存储**

在 `instances/models.py` 添加：

```python
class RemoteInstanceStatus(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    REMOTE_DELETED = "remote_deleted"


class LocalInstance(BaseModel):
    schema_version: Literal[1, 2, 3, 4] = 1
    remote_status: RemoteInstanceStatus = RemoteInstanceStatus.UNKNOWN
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
```

在 `InstanceStore.save` 序列化新字段并把带稳定 session 的记录写为 schema 4；旧 metadata 由 Pydantic 默认值读取。新增显式 `remove(instance_id)`，只接受精确 UUID 目录，并先确认 `metadata.json` 属于当前用户后再删除该目录。

- [ ] **Step 4: 增加 B 客户端方法和同步器**

在 `ControlPlaneClient` 加入：

```python
async def list_owned_instances(
    self, installation: InstallationConfig
) -> InstanceListResponse:
    base_url = validate_server_url(str(installation.server_url))
    async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
        response = await client.get(
            f"{base_url}/api/v1/instances/mine",
            headers={"Authorization": f"Bearer {installation.installation_token.get_secret_value()}"},
        )
        response.raise_for_status()
        return InstanceListResponse.model_validate(response.json())
```

新建 `InstanceSynchronizer`：

- 请求 `/mine` 并建立远程 ID 集合。
- 存在于远程集合的记录设为 `online` 或 `offline`，按 response 的 `online` 字段写入。
- 本地存在、远程不存在的记录设为 `remote_deleted`。
- 请求异常时所有记录保留原状态，只写 `last_sync_error` 和同步时间，并返回可打印结果。
- `prune_candidates` 只返回 `remote_deleted` 且 tmux/bridge 都 down，或本地 tmux/bridge 都 down 的记录。

为 CLI 提供确定的默认构造入口，避免命令层自行拼装依赖：

```python
@classmethod
def from_defaults(cls) -> "InstanceSynchronizer":
    return cls(
        InstanceStore.default(),
        ControlPlaneClient(),
        ConfigStore.default().load(),
    )
```

同步器还必须提供 `print_candidates(candidates)`（只打印 UUID、名称、remote_status 和本地 tmux/bridge 探测结果）以及 `remove_candidates(candidates)`（逐条调用 `InstanceStore.remove`，返回已删除 UUID 列表）；这两个方法不执行进程终止，也不调用 B 的删除 API。

- [ ] **Step 5: 增加 CLI 命令和状态输出**

在 `cli.py` 中增加 `sync` 和 `prune`；实现语义必须等价于：

```python
@app.command()
def sync() -> None:
    installation = ConfigStore.default().load()
    result = asyncio.run(
        InstanceSynchronizer(
            InstanceStore.default(), ControlPlaneClient(), installation
        ).sync()
    )
    typer.echo(result.summary())
    if result.error is not None:
        raise typer.Exit(code=1)


@app.command()
def prune(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    synchronizer = InstanceSynchronizer.from_defaults()
    candidates = synchronizer.prune_candidates()
    if dry_run:
        synchronizer.print_candidates(candidates)
        return
    if not force and not typer.confirm(f"清理 {len(candidates)} 个失效实例？"):
        raise typer.Abort()
    removed = synchronizer.remove_candidates(candidates)
    typer.echo(f"Removed {len(removed)} stale instances")
```

`list` 和 `_status_payload` 增加 `remote_status`、`last_synced_at`、`last_sync_error`；状态行明确区分 `remote=online/offline/remote-deleted/unknown`。`doctor` 继续不删除记录，但在配置存在时调用轻量 B 健康探测，并输出最后同步错误。

- [ ] **Step 6: 写清理和诊断测试并验证**

至少增加以下四个完整测试，并分别断言候选文件仍存在、确认取消不删除、`list --json` 输出远程状态/同步错误、`doctor` 输出 B offline 与本地 tmux/bridge 状态：`test_prune_dry_run_does_not_remove_metadata`、`test_prune_requires_confirmation_unless_force`、`test_list_reports_remote_deleted_and_last_sync_error`、`test_doctor_distinguishes_tmux_down_bridge_down_and_remote_offline`。

Run: `uv run pytest apps/node/tests/test_instance_store.py apps/node/tests/test_instance_sync.py apps/node/tests/test_cli_lifecycle.py apps/node/tests/test_diagnostics.py -q`

Expected: PASS；旧 schema metadata、同步失败、远程删除标记、dry-run 和确认清理均有覆盖。

Commit: `git add apps/node/src/termflow_node apps/node/tests && git commit -m "feat: sync and prune stale node instances"`

## Task 3: 抽取可复用的底部 Toast

**Files:**
- Create: `packages/client-ui/src/components/common/BottomToast.vue`
- Create: `packages/client-ui/src/composables/useBottomToast.ts`
- Modify: `packages/client-ui/src/App.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/views/ComputersView.vue`
- Test: `packages/client-ui/src/components/common/BottomToast.test.ts`
- Test: `packages/client-ui/src/views/ComputersView.test.ts`

- [ ] **Step 1: 写失败测试**

断言 Toast 使用既有 DOM 合同和安全区域样式：

```ts
it('renders a dismissing bottom status toast with success tone', async () => {
  const wrapper = mount(BottomToast, { props: { message: '已授权', tone: 'success' } })
  expect(wrapper.get('[data-bottom-toast]').text()).toBe('已授权')
  expect(wrapper.get('[data-bottom-toast]').attributes('role')).toBe('status')
})
```

- [ ] **Step 2: 实现 Host 和 composable**

`useBottomToast()` 返回 `show({ text, tone })` 和 `clear()`；Toast Host 使用 `runtime.clock.setTimeout/clearTimeout`，默认 3 秒，成功状态 role 为 `status`，错误状态 role 为 `alert`。CSS 复用现有 `.computer-delete-toast` 的固定底部、安全区域和主题变量。

- [ ] **Step 3: 在 App 中挂载并迁移电脑管理**

在 `App.vue` 的 `main` 后渲染 `<BottomToast />`；`ComputersView.vue` 删除本地 `deleteNotice` 定时器，改调用 `useBottomToast().show({ text: '已删除', tone: 'success' })` 和 `{ text: '已添加', tone: 'success' }`。保留现有 `data-delete-notice` 合同，或同步更新对应测试为 `[data-bottom-toast]`。

- [ ] **Step 4: 验证**

Run: `npm exec --workspace @termflow/client-ui -- vitest run src/components/common/BottomToast.test.ts src/views/ComputersView.test.ts --environment jsdom --reporter=dot`

Expected: PASS；桌面端底部居中，移动端避开 bottom nav 和 safe area。

Commit: `git add packages/client-ui/src && git commit -m "refactor: share bottom toast across clients"`

## Task 4: 将 Web C 设备审批移到“已授权客户端”弹窗

**Files:**
- Create: `packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.vue`
- Create: `packages/client-ui/src/components/settings/DeviceAuthorizationApprovalDialog.test.ts`
- Create: `packages/client-ui/src/composables/useDeviceAuthorizationApproval.ts`
- Modify: `packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue`
- Modify: `packages/client-ui/src/views/LoginView.vue`
- Modify: `packages/client-ui/src/views/DashboardView.vue`
- Modify: `packages/client-ui/src/views/DeviceAuthorizeView.vue`
- Test: `packages/client-ui/src/views/LoginView.test.ts`
- Test: `packages/client-ui/src/views/DashboardView.test.ts`
- Test: `packages/client-ui/src/views/DeviceAuthorizeView.test.ts`

- [ ] **Step 1: 先改测试合同**

删除登录页和控制中心的设备授权入口断言，新增设置面板合同：

```ts
expect(wrapper.get('[data-action="authorize-new-client"]').text()).toBe('授权新客户端')
await wrapper.get('[data-action="authorize-new-client"]').trigger('click')
expect(wrapper.get('[data-action="device-approval-dialog"]').exists()).toBe(true)
```

- [ ] **Step 2: 抽取设备码 lookup/approve composable**

`useDeviceAuthorizationApproval` 负责规范化 `ABCD-EFGH`、调用 `deviceAuthorizationPreview`、调用 `decideAuthorization`、处理认证失效并返回 `preview/loading/busy/error/success`。它不负责布局和路由，使独立 `/device` 页面与设置弹窗使用同一逻辑。

- [ ] **Step 3: 创建小型弹窗**

弹窗初始只显示设备码输入框；预览成功后显示客户端名称、平台、权限、过期时间、必要 TOTP 输入和“拒绝/允许此设备”。不在正文重复 Web C 操作说明；帮助内容使用 `title`/悬浮提示。审批成功通过 `useBottomToast` 显示“已授权”，触发 `approved` 事件。

- [ ] **Step 4: 修改入口和页面复用**

`LoginView.vue` 删除 RouterLink；`DashboardView.vue` 删除 page-heading 的“设备授权”。`AuthorizedClientsPanel.vue` 在标题右侧增加按钮，弹窗成功后 `load()` 刷新活动客户端列表。`DeviceAuthorizeView.vue` 改用 composable，保留直接 URL `/device?code=ABCD-EFGH` 兼容性，但不再作为登录页入口。

- [ ] **Step 5: 响应式和视觉验证**

在 `app.css` 增加弹窗宽度、二维码分栏和窄屏折叠规则，保持 `main` 内滚动、header/nav 固定。使用已确认的布局：Tauri 设备页左侧底部居中“返回连接/重新生成”，二维码主题色，设备码旁复制 SVG。

- [ ] **Step 6: 验证**

Run: `npm exec --workspace @termflow/client-ui -- vitest run src/views/LoginView.test.ts src/views/DashboardView.test.ts src/views/DeviceAuthorizeView.test.ts src/components/settings/DeviceAuthorizationApprovalDialog.test.ts --environment jsdom --reporter=dot`

Expected: PASS；登录页和控制中心无入口，设置弹窗完成审批并触发共享 Toast。

Commit: `git add packages/client-ui/src && git commit -m "feat: move device approval into authorized clients settings"`

## Task 5: 完成 Tauri 两条授权路径和凭据恢复

**Files:**
- Modify: `packages/client-core/src/auth/ports.ts`
- Modify: `packages/client-core/src/auth/nativeAuthorization.ts`
- Modify: `packages/client-core/src/auth/deviceAuthorization.ts`
- Create: `packages/client-core/src/auth/authorizationState.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.ts`
- Modify: `apps/clients/tauri/src/adapters/tauriAuthorization.ts`
- Create: `apps/clients/tauri/src/adapters/tauriCredentialVault.ts`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue`
- Modify: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`
- Modify: `apps/clients/tauri/src/router.ts`
- Modify: `apps/clients/tauri/src/runtime.ts`
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`
- Modify: `apps/clients/tauri/src-tauri/src/lib.rs`
- Test: `packages/client-core/src/auth/authorizationState.test.ts`
- Test: `packages/client-core/src/auth/nativeAuthorization.test.ts`
- Test: `packages/client-core/src/auth/deviceAuthorization.test.ts`
- Test: `apps/clients/tauri/src/adapters/tauriAuthorization.test.ts`
- Test: `apps/clients/tauri/src/views/NativeConnectView.test.ts`
- Test: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`
- Test: `apps/clients/tauri/src/security-contract.test.ts`

- [ ] **Step 1: 先覆盖授权状态和取消行为**

新增测试要求：

```ts
const states: AuthorizationState[] = []
const session = createAuthorizationStateMachine({ onState: (state) => states.push(state) })
session.requesting(); session.pending(); session.approved(); session.connected()
expect(states).toEqual(['requesting', 'pending', 'approved', 'connected'])
```

设备码测试必须证明 `cancel()` 后不会再调用 `vault.replace`，重新生成前一个 session 的 `authorize()` 被取消，且 `slow_down` 会增加 5 秒间隔。

- [ ] **Step 2: 实现共享状态适配**

为浏览器回调和设备码会话增加 `onState?: (state) => void`；所有异常先转换为 `AuthorizationTerminalState`，再由视图映射为用户文案。共享核心继续注入 `sleep`，不得在 `client-core` 直接调用平台 timer。

- [ ] **Step 3: 补齐 Tauri keyring/恢复边界**

Rust 继续保存 refresh token、P-256 私钥和内存 access token。增加受控的 `native_clear_credentials` command 供注销使用；如新增 access 读取 command，返回值只包含 `AccessCredential`，不得返回 refresh token 或私钥。前端 vault adapter 通过 `invoke` 调用受控 command，不能读取 keyring 文件。

确保 `native_exchange_authorization` 和 `native_exchange_device_code` 的 IPC 入参/返回值全部为 owned 类型；运行 `cargo check --all-targets --all-features`，若再次出现 `__tauri_message__` 生命周期错误，先把命令输入聚合为 owned request struct，再在 async 函数内解构，禁止持有 IPC 借用跨 await。

- [ ] **Step 4: 修正浏览器回调链**

`tauriAuthorizationBrowser.waitForCallback` 必须在 `openUrl` 之前完成 listener 注册；回调只接受 `termflow://auth/callback`、匹配 state 且只含 `state`/`transaction_id`。记录以下阶段日志：`connect_started`、`browser_open_started/failed`、`authorization_callback_received`、`authorization_callback_invalid`、`token_exchange_succeeded/failed`。日志中禁止 token、code_verifier、DPoP 私钥和验证码。

- [ ] **Step 5: 重做 Tauri 页面**

`NativeConnectView.vue` 首屏只保留服务器地址和两个并排按钮；浏览器按钮进入等待状态，设备码按钮只路由到 `/connect/device`。错误文案按阶段区分，不再笼统提示“检查 B 和审批状态”。

`NativeDeviceAuthorizeView.vue` 在 `onMounted` 自动请求设备码；进入页面即展示分栏内容。左侧显示地址、审批状态和底部居中的“返回连接/重新生成”；右侧显示 `ThemedQrCode`、设备码及紧邻复制 SVG。页面正文删除“请在已登录的 Web C 中……”等重复说明，保留悬浮帮助。

- [ ] **Step 6: 连接成功后统一导航和 Toast**

两个流程的成功处理都执行：停止 timer/session → 保存/确认凭据 → `useBottomToast().show({ text: '已连接', tone: 'success' })` → `router.replace(redirect || '/')`。路由守卫继续通过实际 `/api/v1/dashboard` 请求确认 token 可用；失败时回到 `/connect?redirect=/computers` 并保留阶段错误。

- [ ] **Step 7: 验证 Tauri 单测和 Rust**

Run: `npm exec --workspace @termflow/tauri-client -- vitest run src/adapters/tauriAuthorization.test.ts src/views/NativeConnectView.test.ts src/views/NativeDeviceAuthorizeView.test.ts --environment jsdom --reporter=dot`

Run: `cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --all-targets --all-features`

Expected: PASS；覆盖回调、设备码、取消、恢复、错误阶段和日志脱敏。

Commit: `git add packages/client-core/src apps/clients/tauri/src apps/clients/tauri/src-tauri/src && git commit -m "feat: complete native authorization state and recovery"`

## Task 6: 真实浏览器、响应式和发布前验证

**Files:**
- Modify: `tests/e2e/test_device_authorization.py`
- Modify: `tests/e2e/conftest.py` only to add isolated Web C browser fixtures
- Modify: `apps/clients/tauri/src/views/NativeConnectView.test.ts` and `NativeDeviceAuthorizeView.test.ts` when browser findings require a contract update
- Verify only: `.github/workflows/tauri-packages.yml` existing Windows/Linux/Android/iOS matrix; no workflow change is part of this plan

- [ ] **Step 1: 启动隔离 B/Web C 并运行 Web 流程**

使用仓库已有的本地验证入口，不向线上服务写数据：

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
UV_DEFAULT_INDEX=https://pypi.org/simple \
./scripts/run-web-e2e.sh
```

验收：管理员 Token 登录后，在设置页打开“授权新客户端”，输入设备码并批准；弹窗关闭、客户端列表刷新、底部 Toast 出现；登录页和控制中心均没有设备授权按钮。

- [ ] **Step 2: 检查桌面和手机布局**

在真实浏览器分别使用约 `1440x900` 和 `390x844` 视口，截图保存到隔离测试产物目录。检查：

```text
首屏：服务器地址与两个按钮在桌面并排、手机堆叠；
设备页：桌面左右分栏、手机上下排列；
返回连接/重新生成只出现一次且位于左侧底部居中；
二维码有主题色；设备码复制 SVG 紧邻设备码；
header/nav 固定，内容区独立滚动。
```

- [ ] **Step 3: 验证 Windows 安装包真实闭环**

安装最新 Windows 包后记录 `termflow-client.log`，分别执行：

1. 浏览器登录，确认深链回调事件和控制中心导航；
2. 设备码授权，在另一台 Web C 设置页批准，确认轮询最终进入控制中心；
3. 重启应用，确认 Rust keyring refresh 能恢复访问；
4. 断开服务器，确认页面显示网络阶段错误且日志不泄露敏感字段。

- [ ] **Step 4: 验证 A 同步和清理**

```bash
termflow sync
termflow list --json
termflow prune --dry-run
termflow doctor
```

先在 B 删除一个 offline Term，再运行 `sync`，确认它显示 `remote_deleted`；`prune --dry-run` 只列候选；确认后 `prune --force` 删除本地元数据；再次 `list/doctor` 不得显示可用实例。

- [ ] **Step 5: 运行聚合验证并记录证据**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
UV_DEFAULT_INDEX=https://pypi.org/simple \
./scripts/verify.sh
```

保存 pytest、Vitest、Rust、Web E2E 和 Tauri 构建输出；只有这些输出及真实 Windows 链路都成功后，才可声称本轮完成。

Commit: `git add tests .github/workflows && git commit -m "test: verify native auth and node sync flows"`

## 执行顺序与检查点

1. Task 1 先完成 B 的 scoped API，保证 A 有安全同步来源。
2. Task 2 完成 A `sync/prune/doctor`，先用假 transport 和本地 metadata 测试。
3. Task 3 抽取底部 Toast，后续 Web/Tauri 都复用同一组件。
4. Task 4 完成 Web C 入口迁移和弹窗审批。
5. Task 5 完成 Tauri 状态、keyring、深链和两页 UI。
6. Task 6 在真实浏览器和 Windows 包上验证闭环；任何一条链路失败都回到对应任务修复，不以单测代替真实验收。

每个 Task 完成后单独提交并运行该 Task 的验证命令；实现阶段必须在独立 worktree 中执行，合并前再运行一次 main 分支的聚合验证。
