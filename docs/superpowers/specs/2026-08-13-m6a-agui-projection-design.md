# M6a：AG-UI C-facing wire projection 设计

**日期：** 2026-08-13
**状态：** 设计 spec（M6 第一项"Evaluate an AG-UI projection"，计划 1014 行；调研已完成，剩实现）
**范围：** B Control Plane（投影模块 + 端点接线 + bounded payload 持久化前置）、测试与验证
**工作树：** `feat/agent-broker-v0.2.0`

## 1. 目标

把 B 的 canonical Agent 事件（`AgentEventKind` 9 种）投影为 **AG-UI 0.1.19** wire 事件（`ag-ui-protocol==0.1.19`，MIT，reuse.py `ADOPT_NARROWLY`），经既有 Agent stream 端点（`GET /api/v1/agent/stream?wire=agui`）与 REST 历史 replay 端点（`GET /api/v1/agent/conversations/{id}/events?wire=agui`）交付给 C。投影保持 B canonical 事件、opaque cursors、auth、retention、approval 语义为 source of truth（计划 1014 行）：AG-UI 事件对象本身永不带 B 游标/序号/digest/auth 字段，闭流与重置语义原样复用。

本任务还解决一个**阻断性前置缺口**：`agent_events` 表目前只有 `payload_digest`（无任何内容），live 与 replay 都没有可投影的 payload，因此本设计包含一个有界 payload 持久化迁移（§4.2）——没有它投影是空转。

**不引入 `ag-ui-protocol` 包依赖**（pin 决策：wire projection only，手写投影层，只对齐 wire 形状；以本地 pinned wire fixture 作为防漂移契约）。

## 2. 非目标

- Web/Tauri Agent transport 与 Agent Chat/审批 UI（M6 后续任务，计划 1016-1018 行）。
- AG-UI 输入路径（`RunAgentInput` / `resume` / interrupts）：C→B 输入保持 B REST 语义；interrupt 建模（`RUN_FINISHED.outcome`）不投影。
- Reasoning / Step / Activity 事件：B canonical 无此模型（M4.2 适配器显式丢弃 reasoning），永不投影。
- `packages/client-contracts/src/generated.ts` 变更：AG-UI wire 类型不是 B 的 Pydantic 契约面（§4.7）。
- REST `/messages` 端点暴露 payload（C 历史渲染属 M6 UI 任务，本设计只保证事件投影路径内容完整）。
- retention 机制本身：`agent_events` 的 30 天 durable-timeline / 24 小时 checkpoint purge 尚未实现（既有 gap，见 §10 风险 4）；payload 列的生命周期 = 行生命周期，不新增 purge。
- 既有行的 payload 回填：迁移前历史行 `payload=NULL`，在 agui 投影下显式丢弃并计数（0.2.0 尚未发布，可接受）。
- 新配置项、新端点、新表（除 §5 的列与 fixture 外）。

## 3. 背景与现状

- **计划**：M6 1014 行"Evaluate an AG-UI projection for C message/tool/state events; keep B canonical events, opaque cursors, auth, retention, and approval semantics as the source of truth"；M6 1019 行要求验证 "offline replay, cursor resume, duplicate deltas, slow client recovery"——这些验证在 agui 模式下同样必须成立。
- **调研结论**（reuse.py 179-197 行）：AG-UI `ADOPT_NARROWLY`，reuse_boundary="C-facing projection of run/message/tool/state events"，pin `ag-ui-protocol==0.1.19`（MIT，pydantic>=2.11.2）；watch 项：1.0.0 的 THINKING→REASONING 迁移。reuse.py 的 contract_fixture URL（`docs.ag-ui.com/api-reference/openapi.json`）实测 404，本任务改为本地 pinned fixture（§7）。
- **Agent stream 端点**（`api/agent_stream.py` + `plugins/agent_broker/agent/stream_hub.py`，M6.2 已审查）：SSE 三帧 `agent_event`/`reset`/`closed`；opaque cursor `{epoch}-{database_seq}`；auth epoch 与 binding revocation 闭流（4401/4412）、slow consumer（4410）；subscribe-before-replay + `database_seq` 去重；hub 的 `asyncio.Queue[AgentEvent]` 携带 ORM 行。
- **关键现状缺口**：`agent_events`（models.py 561-591 行）与 `agent_messages` 只有 `payload_digest`/`body_digest`，无内容列（0006 迁移一致）。M4.5 spec（"消息正文/事件 payload 的原文持久化……文本经 live hub 传递，C 端历史渲染属 M6"）把内容交付推迟到 M6——而 hub 实际只传 digest-only 行。因此今天 C 无论 live 还是 replay 都拿不到 message text / tool 名 / run 状态内容；M6a 必须先在 B 侧解决 payload 可得性。
- **canonical payload 模型**（`packages/protocol/src/termflow_protocol/agent.py`）：9 种 `AgentEventKind`（`run_started`/`message_delta`/`message_completed`/`tool_started`/`tool_completed`/`permission_requested`/`run_completed`/`run_failed`/`backend_state_changed`），payload 字段全部有界（text ≤8192、error_message ≤4096、evidence ≤4096 等）。
- **M4.5 pipeline 尚未实现**（本 worktree 无 `pipeline.py`；`append` 目前只被测试调用）。M6a 交付存储列 + 投影 + 端点接线；M4.5 实现落地时把 canonical payload JSON 传入 `append`（签名与不变量见 §5）。
- **测试模式**：`test_agent_stream.py` 的 `_GeneratorStream` 在 TestClient portal 内直驱 `stream_events_generator`；`test_agent_migrations.py` 有 `HEAD = "0007"` 常量与 exact-schema 断言。
- **AG-UI 0.1.19 wire 事实**（从 docs.ag-ui.com 与 PyPI `ag-ui-protocol` 0.1.19 核实）：事件判别值为大写 snake（`"TEXT_MESSAGE_CONTENT"` 等）；Python SDK 序列化为 **camelCase**（`threadId`/`runId`/`messageId`/`toolCallId`/`toolCallName`/`rawEvent`/`activityType`/`stepName`）；`BaseEvent` 含可选 `timestamp`（int）/`rawEvent`；`RunStartedEvent{threadId, runId, parentRunId?, input?}`、`RunFinishedEvent{threadId, runId, result?}`、`RunErrorEvent{message, code?}`；`TextMessageStartEvent{messageId, role:"assistant"}`、`TextMessageContentEvent{messageId, delta非空}`、`TextMessageEndEvent{messageId}`；`ToolCallStartEvent{toolCallId, toolCallName, parentMessageId?}`、`ToolCallResultEvent{messageId, toolCallId, content:str, role?:"tool"}`；`StateDeltaEvent{delta: RFC6902 patch 数组}`；`CustomEvent{name, value}`；`TEXT_MESSAGE_CHUNK{messageId?, role?, delta?}` 是文档定义的 convenience 事件（客户端自动展开为 Start/Content/End），但其类未列入 0.1.19 的 `Event` 判别联合（文档不一致，见 §10 风险 2）。

## 4. 架构设计

### 4.1 投影位置：B 侧（server 端投影）

候选：

- **A. B 侧投影**（本设计）：`agent_stream.py`/`agent_conversations.py` 内用纯函数投影模块把 canonical 事件转为 AG-UI 事件。
- B. C 侧投影：client-core（TS）与 Tauri（Rust）各自实现映射。

论证选 A：

1. **source of truth 一致性**：auth epoch、binding revocation、approval、retention、opaque cursor 全部在 B；投影放 B，端点既有门禁（require_admin、403 binding_revoked、4401/4412、cursor 校验）对 agui wire 原样生效，C 无授权逻辑。
2. **客户端家族多样性**：Web（TS）+ Tauri（Rust）若各自投影 = 两套映射 + 两套测试 + 漂移风险；B 侧一次实现一次测试，两个 transport（后续任务）消费同一 wire。
3. **payload 可得性**：payload 在 B；C 侧投影需要把内容推给客户端（更大的改动），且 replay 路径同样要投影——等于把内容持久化问题搬到客户端。
4. **决策记录**：reuse.py 的 AG-UI 条目 `sbom_owner=termflow-control-plane`、`required_port=AGENT_BACKEND`，边界在 B。
5. AG-UI 是 server→UI wire 契约，server 是自然投影点；通用 AG-UI 工具也能消费该端点。

### 4.2 前置决策：bounded payload 持久化（迁移，编号见 §5）

投影需要 payload（text / tool_name / run 状态），现状 digest-only。候选：

- **A. `agent_events.payload` 可空 TEXT 列**（本设计）。
- B. 仅 live 投影、不落库：replay / `reset` / 4410 恢复无内容，违反计划 1019 行 "offline replay, cursor resume" 验证。
- C. C 侧投影 + payload 推送：见 §4.1 论证 3。

设计要点：

- `agent_events` 增 `payload TEXT NULL`；`append(payload_json: str | None = None)`，提供时校验 `len(payload_json.encode("utf-8")) <= MAX_AGENT_EVENT_PAYLOAD_BYTES`（64 KiB，与 `MAX_CONTEXT_BYTES` 对齐）。
- **digest 不变量**：提供 payload 时强制 `payload_digest == sha256(payload_json.encode())`（fail fast，`ValueError`）。M4.5 已定义 digest 语义为 `sha256(canonical payload json)`——存储与哈希同一字节串，digest 保持可验证，防止 payload/digest 漂移。
- `ephemeral`（MESSAGE_DELTA）也存 payload：live 文本流需要（hub 队列行自带列）。
- 生命周期 = 行生命周期（不新增 purge；30d/24h purge 属既有 gap，§10 风险 4）。
- 既有调用（payload=None）行为不变；既有行 `payload=NULL` → agui 投影丢弃 + 诊断（§4.6）。

### 4.3 投影模块与映射表

新模块 `plugins/agent_broker/agent/agui_projection.py`：

```python
MAX_AGENT_EVENT_PAYLOAD_BYTES = 64 * 1024
AGUI_PROTOCOL_VERSION = "0.1.19"
AGUI_STATE_PATH = "/backend"                      # STATE_DELTA patch 路径
AGUI_PERMISSION_CUSTOM = "termflow.permission_requested"

def project_agent_event(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    """Stateless, pure mapping; returns zero or more AG-UI event dicts."""
```

`AgentEventProjector`（端点注入的类型）是**每连接一个的薄包装**：持有丢弃计数器（§4.6）并委托给纯函数，不引入任何跨事件状态——重放路径新建实例即可，状态安全由 stateless 映射保证。

**stateless、每 B 事件 → 至多一个 AG-UI 事件**：cursor 重放与 REST 分页 replay 都要求投影与历史顺序无关（不维护跨事件状态）。因此 message 流采用 **TEXT_MESSAGE_CHUNK**（AG-UI 文档定义的 convenience 事件："omit explicit TextMessageStart and TextMessageEnd"，客户端展开为 Start→Content→End）而不是手动 START/CONTENT 三元组——三元组需要跨事件状态，在分页 replay 下无法保持（详见 §10 风险 2 与决策摘要）。

映射表：

| B `event_kind` | 投影为（AG-UI 0.1.19 wire，camelCase） | 字段映射与说明 |
|---|---|---|
| `run_started` | `RUN_STARTED` | `threadId=str(conversation_id)`、`runId=str(run_id)`、`timestamp`（created_at 毫秒）。`input` 省略（B 事件不携带 input payload）。`run_id is None` → 丢弃 + 诊断（防御；pipeline 恒为 run 事件设 run_id） |
| `message_delta` | `TEXT_MESSAGE_CHUNK` | `messageId=str(message_id)`、`role="assistant"`（B 的 delta 事件恒为后端输出）、`delta=text`、`timestamp`。`text` 为空/None → 丢弃 + 诊断（chunk 要求非空 delta）。`part_id`/`assembly_revision`/`ephemeral` 不投影（装配语义留在 B） |
| `message_completed` | `TEXT_MESSAGE_END` | `messageId=str(message_id)`、`timestamp`。与 chunk 展开的消息配对；客户端对未知 messageId 的 END 应可忽略（replay 边界）。`final`/`assembly_revision` 不投影 |
| `tool_started` | `TOOL_CALL_START` | `toolCallId=tool_call_id`、`toolCallName=tool_name`、`timestamp`。`parentMessageId` 省略——B canonical 无消息↔工具关联（文档记录；C 按 toolCallId 关联） |
| `tool_completed` | `TOOL_CALL_RESULT` | `messageId=tool_call_id`（**合成**：B canonical 无父消息 ID，用 tool_call_id 保持确定性与可关联性，文档记录）、`toolCallId`、`role="tool"`、`content=json.dumps({status, input_bytes, output_bytes, truncated, error_code?, error_message?})`（有界摘要串；B 不存 raw tool 结果，§7；`input_hash`/`output_hash` 不投影）、`timestamp` |
| `permission_requested` | `CUSTOM` | `name="termflow.permission_requested"`、`value={approval_request_id, tool_name?, evidence?, expires_at?}`、`timestamp`。**审批状态机保持 B REST（M5.1/5.2）为 source of truth**；CUSTOM 只是 UI 可见性视图 |
| `run_completed` | `RUN_FINISHED` | `threadId`、`runId`、`timestamp`。无 `result`/`outcome`（0.1.19 无 outcome 字段；interrupt 建模非目标）。`run_id is None` → 丢弃 + 诊断 |
| `run_failed` | `RUN_ERROR` | `message=error_message or error_code`（必填）、`code=error_code`、`timestamp`。`retryable` 不投影（B 语义） |
| `backend_state_changed` | `STATE_DELTA` | `delta=[{"op":"replace","path":"/backend","value":{"state":…,"epoch":…}}]`、`timestamp`。状态文档 schema 文档化：`{"/backend": {"state","epoch"}}`；C 以 REST capabilities 种子初始状态后应用 delta。`epoch` 是 B runtime epoch（经 REST 已对 C 公开，非 storage 内部值） |
| 未知 `event_kind` | （丢弃 + 诊断） | 未来 B 事件对 AG-UI 投影 fail-open（canonical 不受影响），绝不臆造 AG-UI 形状 |
| `payload is None` / payload JSON 损坏 | （丢弃 + 诊断） | 不崩流；canonical 帧不受影响 |

**永不投影的 AG-UI 事件**（B 无对应 canonical 模型）：`STEP_STARTED`/`STEP_FINISHED`、`REASONING_*`、`ACTIVITY_*`、`STATE_SNAPSHOT`/`MESSAGES_SNAPSHOT`（初始状态走 REST）、`TOOL_CALL_ARGS`/`TOOL_CALL_END`（B 无参数流）、`TEXT_MESSAGE_START`/`TEXT_MESSAGE_CONTENT`（用 chunk）、`RAW`、`TOOL_CALL_CHUNK`。

**不泄露不变量**（对照计划 1014 行）：AG-UI 事件对象内永不出现 `database_seq`、`event_id`、`payload_digest`、`dedup_key`、`ephemeral`、auth epoch、cursor；`conversation_id`/`run_id` 以 `threadId`/`runId` 出现——两者已通过 REST 与既有 stream 信封对 C 公开，是 C-facing 身份而非 storage 内部值。测试逐事件扫描断言（§8）。

### 4.4 端点接线：`?wire=agui`，不新增端点

决策：复用 `GET /api/v1/agent/stream`，query param `wire=canonical|agui`（默认 `canonical`，行为与现在逐字节一致）。

- 不新增 `/stream-agui` 端点：会复制订阅/重放/dedup/闭流逻辑或被迫共享生成器参数化；复用既有 `stream_events_generator` 并注入投影器是唯一实现。
- 不用 Accept header：浏览器 `EventSource` 无法设置自定义 header 而可以带 query param；与现有 `cursor`/`conversation_id` 参数风格一致。
- `wire` 未知值 → 400 `invalid_wire`（fail closed）。
- agui 模式帧契约**不变**：`event: agent_event` + `data: {"type":"event","event":<AG-UI 事件对象>,"cursor":"{epoch}-{seq}"}`；`reset`/`closed` 帧原样（B 语义，AG-UI 无对应物）。**AG-UI 事件本身永不带 B 游标**——游标只存在于 B 的 SSE 信封（source of truth 边界）。
- 不可投影事件：不产生帧，但 cursor/seq 簿记照常推进（跳过不卡流、不破坏去重）。
- 实现：`stream_events_generator` 增 `projector: AgentEventProjector | None = None` 参数；replay 与 live 两路统一经 `_frame_for_event(event, cursor) -> str | None` 钩子（`None` = 跳过）；默认 `None` 时行为与现状完全一致。
- dedup、auth epoch 闭流、binding revocation、4410、reset 判定全部复用既有机制，与 wire 无关。
- 全局流（无 conversation_id）在 agui 模式：客户端按 `threadId` 归并事件（threadId 即 conversation_id）。

### 4.5 REST replay 同步：`/events` 增 `wire=agui`

client-core 的 reset/too-slow 恢复走 REST replay（`fetchEventsSince`）。若流是 agui 而 replay 是 canonical，客户端被迫消费两种格式且 canonical 无内容。因此：

- `GET /api/v1/agent/conversations/{id}/events?wire=agui&since=…&limit=…`：响应 envelope 不变（`events`/`next_cursor`，`next_cursor` 语义仍为 `database_seq`），`events` 元素为 AG-UI 事件 dict（stateless 投影天然适配分页——每页独立投影，chunk 语义保证消息完整性）。
- `wire=canonical`（默认）不变。
- `/messages` 保持 canonical（历史渲染属 M6 UI 任务）。

### 4.6 诊断

投影器返回丢弃计数（`unknown_kind`/`empty_delta`/`missing_payload`/`malformed_payload`/`run_without_run_id`），按连接实例累计，丢弃时记 warning 日志（有界诊断、不记 payload 内容——沿用 M4.2 诊断脱敏模式）。不新增表。

### 4.7 client-contracts：不改

`generated.ts` 由 `generate.py` 从 B 的公开 Pydantic 模型渲染；AG-UI wire 类型是 **pinned 外部契约**（`ag-ui-protocol==0.1.19`），不是 B 的模型面。按"wire projection only、不引入包"原则：B 侧手写投影 + 本地 fixture 钉契约；C 侧 TS 的 AG-UI 事件类型在 transport 任务落地时手写于 client-core（本设计非目标）。`AgentEventListResponse` 在 agui 模式下 events 元素为任意 JSON 对象（不套 `AgentEventResponse`），对既有 canonical 客户端零影响。

## 5. 数据与 API

### 迁移 `0009_agent_event_payload.py`

- `ALTER TABLE agent_events ADD COLUMN payload TEXT NULL`。
- 编号：取当前 head（`0007`）之后第一个可用序号；若 M5.2 的 `0008_approval_audit` 已落地则用 `0009`。同步更新 `test_agent_migrations.py` 的 `HEAD` 常量、exact-schema 测试、升级/降级断言。

### Repository

- `AgentEventRepository.append(payload_json: str | None = None)`：校验大小（≤64 KiB）与 digest 不变量（`payload_digest == sha256(payload_json.encode())`，提供时强制）；INSERT 含新列。
- `AgentEventCursor.append` 透传 `payload_json`；`list_since_cursor`/`list_for_conversation` 返回的 ORM 行自动携带 payload（hub 队列与 replay 均直接可用，**stream_hub.py 零改动**）。
- 既有调用（不传 payload）不变。

### API

- `GET /api/v1/agent/stream?wire=…`（§4.4）。
- `GET /api/v1/agent/conversations/{id}/events?wire=…`（§4.5）。
- 错误码：`TermFlowError("invalid_wire", 400, "The Agent stream wire format is invalid.")`。

### 配置

无新 settings（`AGUI_PROTOCOL_VERSION` 等常量内联于投影模块）。

## 6. 安全

- 端点门禁不变：`require_admin`、binding revoked/disabled → 403、auth epoch 闭流、cursor 校验、4410——`wire` 参数只选择帧内容形状，不改变任何授权/生命周期语义。
- 泄露面：投影只输出有界字段（text ≤8192、evidence ≤4096、error_message ≤4096、`content` 摘要 ≤~16 KiB）；不输出 digest/event_id/seq/epoch(storage)/dedup/cursor/auth 字段；不输出 raw tool 结果与 reasoning（B 本就不存）。
- 未知 `wire` 值 fail closed（400）。
- 诊断不记 payload 内容。

## 7. 文件变更

新增：

- `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/agui_projection.py` —— 纯函数投影 + 丢弃计数（§4.3/§4.6）
- `apps/control-plane/src/termflow_control_plane/persistence/migrations/versions/0009_agent_event_payload.py`（文件名编号按 §5 规则：现 head=0007 时取 `0008`，若 M5.2 的 0008 已落地则用 `0009`）
- `apps/control-plane/tests/test_agui_projection.py` —— 纯函数 + 不泄露不变量测试
- `apps/control-plane/tests/fixtures/agui/agui-0.1.19-wire.json` —— pinned wire fixture（每个投影 kind 的期望 JSON，字段名/判别值逐字节钉死）

修改：

- `apps/control-plane/src/termflow_control_plane/persistence/models.py` —— `AgentEvent.payload: Mapped[str | None]`
- `apps/control-plane/src/termflow_control_plane/persistence/repositories.py` —— `append` 增 `payload_json` + 校验
- `apps/control-plane/src/termflow_control_plane/api/agent_stream.py` —— `wire` 参数、`projector` 注入、`_frame_for_event` 钩子
- `apps/control-plane/src/termflow_control_plane/api/agent_conversations.py` —— `/events` 的 `wire` 参数
- `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/reuse.py` —— AG-UI 条目 contract_fixture 404 URL 改为本地 fixture 路径 + SDK 文档 URL
- `apps/control-plane/tests/test_agent_stream.py` —— wire=agui 集成用例（§8）
- `apps/control-plane/tests/test_agent_migrations.py` —— HEAD/exact-schema
- `apps/control-plane/tests/test_agent_repositories.py` —— append payload 用例

**不改**：`stream_hub.py`、`turns.py`、`packages/client-contracts/`、`uv.lock`（零依赖变更）、`plugin.py`（router 已注册；投影器无状态，无需组合根装配）。M4.5 的 `pipeline.py`（尚未存在）实现时把 canonical payload JSON 传入 `append`，本任务在测试中直接驱动 append 验证。

## 8. 测试矩阵

| 验证项 | 测试位置与断言 |
|---|---|
| 投影纯函数：9 种 kind 映射 | `test_agui_projection.py`：每个 kind 输出与 `agui-0.1.19-wire.json` fixture **精确相等**（type 判别值、camelCase 字段名、timestamp 毫秒、无多余字段） |
| 显式丢弃 | 未知 kind → `[]` + `unknown_kind` 计数；空/None text 的 delta → 丢弃 + 计数；`run_started`/`run_completed` 的 `run_id=None` → 丢弃 + 计数；`payload=None` → 丢弃 + 计数；损坏 payload JSON → 丢弃 + 计数且**不抛异常** |
| 不泄露不变量 | 对每个投影输出扫描：`database_seq`/`event_id`/`payload_digest`/`dedup_key`/`ephemeral`/`cursor`/auth epoch 字符串永不出现 |
| 合成与文档化形状 | `TOOL_CALL_RESULT`：`messageId == toolCallId`、`content` 为含 status/input_bytes/output_bytes/truncated（+错误字段）的 JSON 串、无 hash 字段；`CUSTOM` 的 name/value 形状；`STATE_DELTA` 的 patch 路径恰为 `/backend` 且 value 为 {state, epoch} |
| 端点集成（`test_agent_stream.py` 扩展，`_GeneratorStream`） | happy path：append 带 payload → `agent_event` 帧的 `event` 为 AG-UI 形状、`cursor` 仍为 `"{epoch}-{seq}"`；replay from cursor 顺序保持；不可投影事件 → 无帧但**后续事件仍到达**（不卡流）、cursor 越过跳过事件；`wire=canonical` 默认行为回归（帧逐字节不变）；`wire=unknown` → 400 `invalid_wire`；`reset`/`closed` 帧在 agui 模式原样（epoch 不匹配、binding revocation、hub 闭流三例）；dedup（同 dedup_key 双 append）在 agui 模式仍只投一次；全局流（无 conversation_id）按 threadId 可归并 |
| REST replay | `/events?wire=agui`：分页投影、`next_cursor` 语义不变、与流内事件可拼接（同一消息 chunk+END 完整）；`/events` 默认 canonical 回归 |
| 仓储/迁移 | `append` 持久化 payload；digest 不变量：`payload_digest == sha256(payload_json)`（提供时强制，不一致 → ValueError）；>64 KiB 拒绝；`payload=None` 既有调用不变；0009 exact-schema（新列/索引不变/downgrade）；`test_agent_migrations.py` HEAD 更新 |

## 9. 迁移与兼容

- **线上/API**：`wire=agui` 是纯增量 query param；默认 `canonical` 行为不变，既有 C 客户端（client-core frames/stream）零影响。
- **数据库**：0009 只加可空列，无破坏性变更；既有行 `payload=NULL` → agui 投影丢弃 + 诊断（0.2.0 未发布）。
- **依赖**：零新增（不引入 `ag-ui-protocol`；uv.lock 不变）。
- **与 M4.5**：pipeline 实现时把 canonical payload JSON 传入 `append`（M6a 定义签名与 digest 不变量；pipeline 测试沿用）。
- **与 M5.2**：`permission_requested` 投影为 CUSTOM 只读视图，`approval_requests` 状态机/审计（M5.1/5.2）无任何改动。

## 10. 风险与缓解

1. **schema 变更超出"投影"字面范围**：`agent_events.payload` 列是投影的内容前提（现状 digest-only，无内容可投影；M4.5 已把 C 端内容交付推迟到 M6）。缓解：可空列 + `append` 可选参数向后兼容；digest 不变量防漂移；测试矩阵给出拒绝/降级路径；本 spec 将此项明示为 M6a 前置决策，需 reviewer 确认。
2. **AG-UI 0.1.19 文档不一致/协议漂移**（`TEXT_MESSAGE_CHUNK` 不在 0.1.19 `Event` 判别联合但 EventType 与文档均有、1.0.0 THINKING→REASONING 迁移、reuse.py contract_fixture URL 404）：缓解：本地 pinned wire fixture 精确断言每个投影 kind 的 wire 形状（`agui-0.1.19-wire.json`）；选 chunk 是因 stateless 投影在分页 replay 下无法维持 START/CONTENT/END 三元组（chunk 正是 AG-UI 为此定义的 convenience 语义）；若后续协议版本把 chunk 移出 wire，fixture 测试即时暴露，只改投影模块；1.0 迁移是已记录 watch 项。
3. **无 payload 事件的投影空窗**（迁移前行 / 未来调用方不传 payload）：显式丢弃 + 诊断计数，canonical 流不受影响；0.2.0 未发布，无真实历史数据。
4. **payload 列存储增长**（≤64 KiB/事件）与 retention purge 未实现（30d/24h 是计划 §16.1 承诺、当前是既有 gap）：本任务不新增 purge（范围纪律）；行生命周期即 payload 生命周期；风险记录于 spec，留待 §16 retention 任务。
5. **AG-UI 关联字段缺失**（`TOOL_CALL_RESULT.messageId` 合成、`parentMessageId` 省略）：B canonical 无消息↔工具关联，扩字段会动 source of truth（违反 1014 行）；文档记录合成规则，C 按 `toolCallId` 关联。
6. **双格式客户端复杂性**（agui 流 + canonical REST）：`/events` 同步 `wire=agui`（§4.5）使 C 恢复路径统一消费 AG-UI；既有 canonical 客户端不受影响。

## 11. 决策摘要

- **投影位置：B 侧（server 投影）**，纯函数模块 + 端点接线；不采用 C 侧双实现（TS+Rust 两套映射/测试/漂移）。
- **前置：`agent_events.payload` bounded 可空列**（迁移编号见 §5，≤64 KiB，`payload_digest == sha256(payload_json)` 不变量，可选参数向后兼容）——无内容可投影是阻断性缺口。
- **映射**：9 种 canonical kind 全投影（§4.3 表）；`message_delta → TEXT_MESSAGE_CHUNK`（stateless、分页 replay 安全、客户端展开三元组）+ `message_completed → TEXT_MESSAGE_END`；`tool_started → TOOL_CALL_START`；`tool_completed → TOOL_CALL_RESULT`（`messageId=tool_call_id` 合成、`content` 有界摘要）；`permission_requested → CUSTOM termflow.permission_requested`（审批状态机仍 B REST）；`run_started/run_completed/run_failed → RUN_STARTED/RUN_FINISHED/RUN_ERROR`；`backend_state_changed → STATE_DELTA`（`/backend` 路径）；未知 kind/空 delta/缺 payload/无 run_id → 显式丢弃 + 诊断。
- **不泄露**：AG-UI 事件永不带 cursor/seq/digest/event_id/epoch(storage)/dedup/auth 字段；`threadId`/`runId` 用 B 已公开的 conversation_id/run_id。
- **端点**：复用 `/api/v1/agent/stream` 加 `wire=agui`（query param，EventSource 兼容；不新增端点、不用 Accept）；`reset`/`closed`/opaque cursor 信封语义原样；`/events` 同步 `wire=agui` 支持 REST replay 恢复。
- **client-contracts 不改**（AG-UI 类型非 B 契约面；C 侧 TS 类型随 transport 任务手写）；**不引入 ag-ui-protocol 依赖**；本地 pinned wire fixture 为防漂移契约；修正 reuse.py 的 404 contract_fixture URL。
- 零新配置；错误码 `invalid_wire`(400)；stream_hub.py/turns.py/generated.ts 零改动。

## 12. 开放问题

- 迁移编号依赖 M5.2 的 `0008_approval_audit` 落地顺序（取当前 head 后第一个可用序号）。
- REST `/messages` 的 payload 暴露（C 历史渲染）留给 M6 UI 任务，本设计只保证事件投影路径内容完整。
- `agent_events`/`agent_messages` 的 30d/24h retention purge 尚未实现（既有 gap）；payload 生命周期暂 = 行生命周期。
