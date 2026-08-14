# M6b：认证 Web Agent transport + Agent Chat UI + 审批 UI 设计

**日期：** 2026-08-13
**状态：** 设计 spec（M6 剩余 Web 侧任务：计划 1016 行"Add authenticated Web Agent transport"、1018 行"Implement Agent Chat…and accessible approval UI"、1019 行的 Web 验证部分）
**范围：** Web（浏览器 TS/Vue）Agent transport、Agent Chat UI、审批 UI、客户端会话/历史状态、离线 replay 验证
**工作树：** `feat/agent-broker-v0.2.0`

## 1. 目标

让管理员在 **Web 客户端**里完成 Agent Chat 全链路：以既有 admin 会话认证建立 Agent 事件流连接，浏览/创建会话，发送文本（`POST /messages`，202）、取消运行（`POST /cancel`），看到 assistant 消息流、可见工具活动、backend 运行状态与审批上下文，并对 pending 审批执行 approve/deny/revoke；离开后返回（含刷新页面）能无缝恢复历史与 live 流。传输层消费 **M6a 已落地的 `?wire=agui` 投影**（流与 REST replay 两路统一），复用 M6.2 已审查的 client-core `AgentStreamSession` 恢复机制（opaque cursor、4401/4412 闭流、4410 REST 恢复），把"offline replay、cursor resume、duplicate deltas、slow client recovery、auth epoch closure、binding revocation"六项计划 1019 行验证在 Web 侧落地为可执行测试。所有 C 侧实现只依赖既有 REST/SSE 契约，**不新增 B 端点**（除 §6.6 标记的一个跨任务依赖，见开放问题 1）。

## 2. 非目标

- **Tauri Agent transport**（计划 1017 行，Rust-owned HTTP/WS，单独任务）。本 spec 只保证共享 UI 依赖的运行时端口（§4.6）对 Tauri 后续实现开放。
- 全局流（无 `conversation_id`）的 agui 消费：AG-UI 事件无事件 id，全局流无法去重（论证 §4.3）；Chat UI 只开 conversation-scoped 流。既有 canonical 全局流客户端行为不变。
- AG-UI 输入路径（`RunAgentInput`/`resume`）、interrupt 建模、Reasoning/Step/Activity 事件（M6a 已排除，B 不投影）。
- Markdown/富文本渲染与链接自动化：0.2.0 只渲染纯文本（§6.2 安全论证），strict Markdown allowlist 留后续。
- Watch 创建/管理 UI、transcript draft UI（M7 移动端任务）、binding/profile/token 管理 UI（agent_admin 面板非本任务）。
- 审批状态机、audit、MCP 写工具语义（M5.1/M5.2 已定）；Delegated Write Grants UI（capability 恒 False，只读展示）。
- retention purge、Tauri WebView 网络权限、移动端布局适配（桌面/窄屏响应式沿用既有 shell 断点即可）。

## 3. 背景与现状

- **计划**：M6 1016 行"Add authenticated Web Agent transport"；1018 行"Implement Agent Chat, visible tool activity, backend state, and accessible approval UI"；1019 行"Verify offline replay, cursor resume, duplicate deltas, slow client recovery, auth epoch closure, and binding revocation in Web and Tauri"（本 spec 覆盖 Web 部分）；§13.2：共享 client-core 定义 Agent Conversation 与 stream ports、Web adapter 走 browser Cookie/Origin 路径、共享 Vue Agent Chat 只依赖 runtime port、审批 UI 复用既有 accessible dialog/focus/Toast 模式、文本渲染拒绝 HTML/脚本/ANSI-OSC/`javascript:` 链接。
- **B 侧契约已落地**（本 worktree 已审查）：
  - `GET /api/v1/agent/stream?conversation_id=&cursor=&wire=canonical|agui`（`api/agent_stream.py`）：SSE 三帧 `agent_event`（`{"type":"event","event":…,"cursor":"{epoch}-{seq}"}`）/`reset`/`closed`；`closed.code` 4401（auth epoch 变更）/4410（slow consumer）/4412（binding revoked）；subscribe-before-replay + `database_seq` 去重；cursor 不连续（epoch 不匹配/retention 删除/超前）→ `reset` 帧携带新 cursor。
  - `GET /api/v1/agent/conversations/{id}/events?since=&limit=&wire=`（M6a §4.5）：agui 模式下 `events` 为 AG-UI 投影对象、`next_cursor` 仍是 `database_seq` 语义。
  - AG-UI 投影映射（M6a §4.3 表）：`run_started→RUN_STARTED`、`message_delta→TEXT_MESSAGE_CHUNK`、`message_completed→TEXT_MESSAGE_END`、`tool_started→TOOL_CALL_START`、`tool_completed→TOOL_CALL_RESULT`（`messageId=tool_call_id` 合成、`content` 为有界摘要 JSON）、`permission_requested→CUSTOM termflow.permission_requested`（value 含 `approval_request_id/tool_name?/evidence?/expires_at?`）、`run_completed→RUN_FINISHED`、`run_failed→RUN_ERROR`、`backend_state_changed→STATE_DELTA`（path `/backend`，value `{state, epoch}`）。wire 为 camelCase 字段 + 大写 snake `type` 判别值；AG-UI 事件对象**永不携带** cursor/seq/digest/event_id——游标只存在于 B 的 SSE 信封。
  - 审批 REST（`api/agent_approvals.py`）：`GET /approvals[?conversation_id=]`、`GET /approvals/{id}`、`POST /approvals/{id}/decide`、`POST /approvals/{id}/revoke`；`ApprovalResponse` 含 M5.2 显示字段 `pane_id/operation/intent_summary`（可空，历史行降级 None）+ `state/expires_at/canonical_hash/auth_epoch`；错误码 409（revoked/already_decided/already_consumed/auth_epoch_stale）、410（expired）、404。
  - `GET /api/v1/agent/capabilities`（无需认证）：`agent_broker_enabled`、`delegated_write_grants_enabled`（恒 False）。
  - `GET /api/v1/agent/admin/bindings`：binding 列表（`binding_id/profile_id/term_id/status/runtime_ref/runtime_epoch/…`）。
  - M4.5（尚未落地，本 spec 的依赖）：`POST /api/v1/agent/conversations/{id}/messages`（`{text}` ≤64 KiB `validate_plain_text`，202 `{message_id, conversation_id, admission_seq, idempotency_key, delivery_state, submission_state}`；404/403 `binding_revoked`/503 `binding_runtime_unavailable`/422）与 `POST …/cancel`（202 `{outcome: confirmed|unknown, run_state}`；409 `no_active_run`）。
- **client-core 既有（M6.2，已审查，不重做）**：`agent/ports.ts`（`AgentStreamTransport/AgentStreamConnection/AgentStreamScheduler` + 三帧传输事件 + 4401/4410/4412 常量）、`agent/frames.ts`（`parseAgentStreamFrame`，canonical wire 信封解析）、`agent/stream.ts`（`AgentStreamSession`：connect/reconnect 指数退避、cursor 传递、4401→`onAuthenticationRequired` 且不重连、4412→终态 `onClosed`、4410→`replay(sinceSeq)` REST 恢复后重连、`reset`→刷新 cursor + replay、按 `event.database_seq` 去重、global 流按 event_id 去重）、`api/agents.ts`（`createAgentsApi` + `fetchEventsSince` canonical 分页）。
- **关键现状缺口**（本 spec 必须解决）：
  1. **无浏览器 transport 实现**：`AgentStreamTransport` 是纯 port，Web 侧没有实现（EventSource 与 fetch-stream 的选择见 §4.2）。
  2. **无 agui 消费路径**：`frames.ts` 只解析 canonical `AgentEventResponse`；AG-UI 事件**不带 `database_seq`**，现有 session 的 seq 去重与 replay 拼接依赖事件体内的 seq——必须改为从**信封 cursor**（`{epoch}-{seq}`）取 seq。
  3. **无客户端历史/状态模型**：没有 conversation/history/live-event 状态层（M6.1 计划词条"client-core state"仍未做）。
  4. **generated.ts 缺类型**：`Approval*`、M4.5 的 submit/cancel 模型尚未进入 `generated.ts`（已核实：grep 无 Approval 类型；`generate.py` 未 import `agent_approvals`）。
  5. **用户消息历史不可渲染**：`agent_messages` 只存 `body_digest`（M4.5"文本不落库"），canonical 事件 9 种不含 user message——刷新后用户消息文本在 B 侧无任何来源（§6.6 跨任务依赖）。
- **UI 既有模式**：`client-ui` Vue3 组件 + `clientRoutes` 路由（`requiresAuth` meta）+ `ClientRuntime` 端口（`runtime.ts`，`useClientRuntime()` 注入）+ `createApiClient(createBrowserHttpTransport())`（fetch、`credentials:'same-origin'`、相对路径）+ `useSession`（`refreshSession/clearSessionState`）+ `BottomToast`（`role=status/alert`）+ `ClosePaneDialog` 焦点陷阱模式 + `a11y-contract.test.ts`（skip link/landmark/focus trap/焦点恢复/reduced-motion 断言）+ `privacy-contract.test.ts`（localStorage/sessionStorage/URL/console 不泄漏内容）+ e2e（Playwright，env 门控 `TERMFLOW_E2E_*`）。

## 4. 架构设计

### 4.1 总览与分层

```
┌─ client-ui（Vue，共享 Web/Tauri）
│   AgentView(/agent) AgentChatView(/agent/:conversationId)
│   components/agent/*  composables: useAgentConversation/useAgentConversations/
│   useAgentApprovals/useAgentBroker          ← 只依赖 ClientRuntime 端口
├─ client-core（TS，无框架）
│   agent/agui.ts（手写 AG-UI 类型+校验器）  agent/aguiFrames.ts（agui 信封解析）
│   agent/history.ts（纯 reducer 状态机）     agent/cursorStore.ts（cursor 持久化端口）
│   agent/stream.ts（泛化 wire）  api/agents.ts（submit/cancel/agui replay）
│   api/approvals.ts（新增）
└─ web（浏览器 adapter）
    adapters/browserAgentStreamTransport.ts（fetch-stream + SSE 行缓冲）
    adapters/browserAgentCursorStore.ts（localStorage）  runtime.ts 接线
```

数据流：`B agent stream ?wire=agui → browserAgentStreamTransport（行缓冲）→ parseAgentStreamFrameAgui → AgentStreamSession（seq 去重/恢复）→ useAgentConversation → applyAguiEvent reducer → Vue reactive 状态 → 组件渲染`。REST 恢复与冷启动走 `fetchEventsSinceAgui`（`/events?wire=agui` 分页）同一 reducer 入口。

### 4.2 Web transport：fetch-stream（ReadableStream），不用 EventSource

候选：

- **A. EventSource**：浏览器原生 SSE 解析，自带自动重连；但**读不到 HTTP 状态码**（401/403/404 一律表现为网络错误）、自动重连与 `AgentStreamSession` 自己的 cursor 重连/退避互相打架（URL 里的旧 cursor 会重复 replay，且无法在重连时更新 cursor）、无法用 AbortController 精确取消。
- **B. fetch + ReadableStream 手写 SSE 行缓冲**（本设计）：完全控制重连与取消；**初始 HTTP 状态可见**——这是决定性因素：`require_admin` 会话过期返回 401、binding revoked 返回 403、会话删除返回 404，只有 fetch 能区分并把它们映射为终态闭流（EventSource 只能重连空转）。

设计要点：

- URL：相对路径 `/api/v1/agent/stream?wire=agui`（+`&conversation_id=`、+`&cursor=`，全部 `encodeURIComponent`）；`credentials:'same-origin'`、`Accept: text/event-stream`（沿用 `browserHttpTransport` 同源 Cookie 模式，无新认证面）。transport 按 wire 构造（`createBrowserAgentStreamTransport({ wire: 'agui' })`），`AgentStreamConnectRequest` 不变。
- **初始状态映射**（非 200 时 emit `{type:'close', code, reason}` 后结束）：`401 → code 4401 reason 'authentication_required'`（session 已把 4401 处理为 `onAuthenticationRequired` 终态，复用）；`403 → 4412 'binding_revoked'`；`404 → 4412 'conversation_not_found'`；`400 → 4412 'invalid_cursor'`（客户端持久化 cursor 损坏属客户端 bug，终态优于无限重连）；其余 4xx/5xx → `1006 'http_error'`、网络错误/响应中断 → `1006 'transport_error'`（均可重连）。
- **SSE 行缓冲**：`TextDecoder(stream:true)` 增量解码（多字节 UTF-8 跨 chunk 安全），按 `\n\n` 切完整帧，残余行留缓冲；每帧交给 `parseAgentStreamFrameAgui`（§4.3），解析失败帧**静默丢弃**（不记日志内容，与 canonical 解析器一致）；`response.ok` 后立即 `emit({type:'open'})`。
- `close(code, reason)`：`AbortController.abort()` + `reader.cancel()`，幂等。
- 不在 transport 层做重连（`AgentStreamSession` 的 scheduler 已负责退避 + cursor 恢复）；EventSource 的自带 retry 不存在于 fetch 模型，天然消除双重重连问题。

### 4.3 wire 泛化：seq 取自信封 cursor，replay 改批式水位（agui 事件无 seq 的核心约束）

**问题一**：AG-UI 事件对象不带 `database_seq`，而现有 `AgentStreamSession` 的 conversation 去重/拼接依赖事件体内的 `database_seq`。**解法**：seq 一律从**信封 cursor**（`{epoch}-{seq}`）解析——B 保证每个 `agent_event` 帧的 cursor seq 分量即该事件的 `database_seq`（canonical 与 agui 一致），且 global 流的 cursor 恒为 `{epoch}-0`。

**问题二（更隐蔽）**：REST replay（`/events?wire=agui`）的 `events` 元素也是 AG-UI 对象、**同样无 seq**——每页只有 `next_cursor` 给出"本页覆盖到哪个 seq"。且投影会丢弃事件（不产生 AG-UI 对象但 seq 照推进），同一页内事件的 seq **不连续**、无法逐事件归因。因此 canonical 的 `deliverReplayEvent`（逐事件 `database_seq <= lastSeq` 丢弃）**在 agui 下不可能实现**。解法：**批式水位**——REST replay 按"批次"交付：`{events: AguiEvent[], coveredThrough: number}`（`coveredThrough` = 分页终止时的 `next_cursor`），一次把 `lastSeq` 推进到 `coveredThrough` 再整体投递批次内事件；期间到达的 live 帧**缓冲不投递**，replay 结束后丢弃缓冲中 `cursorSeq <= coveredThrough` 的帧（REST 已完整覆盖该 seq 区间，投影确定性保证同内容），其余按序投递。正确性依据：seq 与提交序单调一致——replay 终止页之后才提交的事件 seq 必 `> coveredThrough`，故"缓冲中 seq `<=` 水位丢弃"精确无 gap 无重复。

具体改动（对已审查代码的最小侵入）：

- `agent/stream.ts`：
  - 事件负载泛化为 `AgentStreamTransportEvent<TEvent>`；conversation 模式 live 去重/`lastSeq` 推进改用 `parseCursorSeq(cursor)`；canonical 模式保留 `event.database_seq <= lastSeq` 检查作为**交叉校验**（不变量：两者相等，不等即视为重复丢弃）；global 模式仍按 event_id 去重且**仅支持 canonical**（agui global 无 id 无法去重，见 §2 非目标）。
  - agui 模式的 replay 走**批式水位路径**：`replayAgui: (conversationId, sinceSeq) => Promise<{events: AguiEvent[], coveredThrough: number}>`（与 canonical 的 `replay: (...) => Promise<AgentEventResponse[]>` 并存，按 wire 选一）；`beginReplayAgui/投递批次/结束`三段中 live 帧进缓冲、结束时按水位过滤投放；`reset`（流保持打开，replay 期间 live 帧同样缓冲）与 4410（流已关闭，无缓冲需求）复用同一机制。
  - `seedFromSeq?: number` 连接选项：冷启动时 session 在 `open` 后自行发起批式 replay（seed），把冷启动 merge 收敛进 session 单一职责（§4.4）。
- `agent/agui.ts`（新增，手写）：AG-UI 0.1.19 wire 的 TS 判别联合（`type` 判别值为大写 snake、字段 camelCase，与 M6a §4.3/§4.4 一致，**不进 generated.ts**，M6a §4.7 已定）：`RUN_STARTED{threadId,runId,timestamp?}`、`TEXT_MESSAGE_CHUNK{messageId,role:'assistant',delta,timestamp?}`、`TEXT_MESSAGE_END{messageId,timestamp?}`、`TOOL_CALL_START{toolCallId,toolCallName,timestamp?}`、`TOOL_CALL_RESULT{messageId,toolCallId,content,timestamp?}`、`CUSTOM{name,value,timestamp?}`、`RUN_FINISHED{threadId,runId,timestamp?}`、`RUN_ERROR{message,code?,timestamp?}`、`STATE_DELTA{delta,timestamp?}`。`parseAguiEvent(value: unknown): AguiEvent | null` 逐字段结构校验（非空 delta、字符串 id、`delta` 数组等），未知/畸形 → `null`（丢弃，绝不崩溃）。
- `agent/cursorStore.ts`（新增）：`parseCursorSeq(cursor): number | null` 与 `isValidAgentCursor(cursor): boolean`（`{epoch}-{seq}` 格式，`epoch>=1`、`seq>=0`，与 `frames.ts` 内部规则一致，抽到共享处供 session 与 cursorStore 使用；`frames.ts` 不改）。
- `agent/aguiFrames.ts`（新增）：`parseAgentStreamFrameAgui(frame)` —— 信封解析复用 `frames.ts` 的三帧逻辑，`agent_event` 帧的 `event` 走 `parseAguiEvent`；`reset`/`closed` 帧与 canonical 完全一致（B 语义，M6a §4.4）。canonical `parseAgentStreamFrame` **不改**（字节级回归安全）。

### 4.4 冷启动/热恢复序列与 cursor 持久化

B 的 subscribe-before-replay 只在**客户端携带 cursor** 时才回放缺口；冷启动（无持久化 cursor）时流是 live-only，且客户端不知道 auth epoch，无法自行铸造 `{epoch}-{seq}` cursor。因此：

- **冷启动序列**（无持久化 cursor）：session 以 `seedFromSeq: 0` 连接（无 cursor，live-only，服务器已订阅）→ `open` 后 session 发起批式 REST replay（0 → `coveredThrough`）→ 期间 live 帧缓冲 → replay 结束后按水位丢弃/投放（§4.3 问题二机制）→ 进入纯 live 模式。**seed 期间用户看到的是增量填充的历史 + 流式 tail，无需任何 spinner 阻断**（reducer 幂等，投递顺序 = seq 顺序）。
- **热恢复序列**（有持久化 cursor）：直接带 cursor 订阅 → 服务器 replay 缺口（live 帧自带 cursor，session 按 `parseCursorSeq` 去重）→ 无 REST 调用。
- **cursor 持久化**：`agent/cursorStore.ts` 定义端口 `AgentCursorStore { load(conversationId): {cursor: string, seq: number} | null; save(conversationId, cursor, seq): void; clear(conversationId): void }`；web adapter `browserAgentCursorStore` 用 localStorage，key `termflow.agent.cursor.<conversationId>`，值 `{cursor, seq}` JSON；读取时用 `isValidAgentCursor` 校验且 seq 非负，非法即丢弃（视同冷启动）。**正确性不依赖持久化**（冷启动 seed 是兜底路径）；epoch 变更后旧 cursor 由服务器 `reset` 帧刷新（session 已处理）。清除时机：会话删除成功、收到 4412、收到 `conversation_not_found`；logout 时不清（其他管理员 session 仍可用）。
- `reset`（cursor_too_old）/4410 恢复：沿用 session 既有 `runReplay/recoverThroughReplay` 触发点，agui 模式走批式水位路径（§4.3）；4410 恢复后重连仍用旧 cursor 字符串（epoch 前缀已知），服务器重放区间被 `lastSeq`（已由 REST 水位推进）丢弃，**REST 已投递内容不重复**。

### 4.5 客户端历史状态机（client-core `agent/history.ts`，纯 reducer）

无框架、纯函数、可单测。状态模型：

- `messages: Map<messageId, {messageId, role:'assistant'|'user', text, status:'streaming'|'complete', createdAt}>`：`TEXT_MESSAGE_CHUNK` 未知 id → 新建；已知 → 追加 delta；`TEXT_MESSAGE_END` → `complete`（幂等）；**`complete` 消息收到 chunk → 忽略**（replay/live 竞态防御）；`TEXT_MESSAGE_END` 未知 id → 忽略（M6a §4.3 replay 边界允许）。
- `toolCalls: Map<toolCallId, {toolCallId, toolName, status:'running'|'completed'|'failed', summary, startedAt, endedAt}>`：`TOOL_CALL_START` 新建；`TOOL_CALL_RESULT` 更新（`content` 摘要 JSON 解析失败则整体作为纯文本 summary，status 按 `error_code/error_message` 有无判 `failed`）；RESULT 未知 id → 补建记录（防御）；completed 重复 RESULT → 忽略。
- `runs: Map<runId, {runId, status:'active'|'finished'|'error', errorCode?, errorMessage?, startedAt, endedAt}>`：`RUN_STARTED` 新建 active；`RUN_FINISHED/RUN_ERROR` 更新（未知 runId 补建）。
- `permissions: Map<approvalId, {approvalId, toolName?, evidence?, expiresAt?, state:'pending'|'decided'|'expired'|'unknown', decidedAt?}>`：`CUSTOM name==='termflow.permission_requested'` upsert 可见性视图；审批真实状态由 REST detail 刷新（M6a：审批状态机 B REST 为 source of truth）；state 初始 `pending`。
- `backend: {state: BackendRuntimeState | null, epoch: number | null}`：`STATE_DELTA` 中 `/backend` 路径 patch → 覆盖 `{state, epoch}`；初始 null（UI 显示"未知"）；**capabilities REST 是 UI 门**（`agent_broker_enabled` 决定导航/路由可见性），backend 运行时状态由 STATE_DELTA（replay 会带历史值）驱动——没有独立 backend-state REST 端点。
- `timeline: TimelineItem[]`：`{type:'message'|'tool'|'permission'|'run'|'user', refId, at}`，首个事件到达时追加；**顺序 = 到达顺序 = 服务器 seq 顺序**（live 与 replay 均保序），时间显示用事件自带 `timestamp`（毫秒）否则创建时间。
- `userMessages: Map<clientId, {clientId, text, deliveryState:'pending'|'accepted'|'rejected', error?}>`：提交本地回显（§4.8）。**seed 与回显时序分离**：历史用户消息只在视图挂载时由 `/messages` 一次性注入（`seedUserMessages`），之后的本会话回显必是新消息，两者无需按 id 合并（202 的 `message_id` 是 inbox 项 id、`/messages` 的 `message_id` 是 `agent_messages` 行 id，双 id 空间不可对齐——以时序分隔回避该问题；离开视图再返回 = 重新 seed，回显文本若已落库则经 body 重新注入，若 §6.6 依赖未落地则降级为占位行）。

`applyAguiEvent(state, event)` 幂等（重复/乱序安全），`seedUserMessages(state, messages: AgentMessageResponse[])`（§6.6 body 依赖）注入历史用户消息。

### 4.6 ClientRuntime 端口扩展（Tauri 可复用）

共享 UI 只依赖 runtime 端口（计划 §13.2）。`ClientRuntime` 增两个成员：

- `createAgentStream: () => AgentStreamTransport` —— Web 注入 fetch-stream adapter（§4.2）；Tauri 后续注入 Rust-owned transport，共享 UI 零改动。
- `agentCursorStore: AgentCursorStore` —— Web 注入 localStorage 实现；Tauri 后续注入自己的存储。

其余（api、toast、session）全部复用既有端口。`fakeRuntime.ts` 同步补桩。

### 4.7 Chat UI（client-ui，Vue）

**路由**（`router/routes.ts`，`requiresAuth: true`）：

- `/agent` → `AgentView.vue`（总览）：binding 选择（`GET /agent/admin/bindings`，显示 term/profile/status）→ 会话列表（`GET /conversations?binding_id=`，标题或"会话 <前 8 位>"）+ 创建（POST，binding + 可选 title）+ 删除（DELETE + 确认弹窗，沿用 ClosePaneDialog 焦点陷阱模式）+ **每会话 pending 审批角标**（`GET /approvals` 全量按 `conversation_id` 分组计数，`state==='pending'`）。
- `/agent/:conversationId` → `AgentChatView.vue`：会话详情头（binding/term + `AgentBackendStatus` 状态徽标 + 运行中取消按钮）、消息流、审批面板、输入框。
- **capability 门**：`useAgentBroker()` 一次性 `GET /capabilities`（无需认证）；`agent_broker_enabled=false` 时导航隐藏 Agent 入口、`/agent*` 视图显示"Agent Broker 未启用"占位（不重定向，端点无认证可探）；`delegated_write_grants_enabled` 恒 False 只读展示"Delegated Write Grants：不可用"。`App.vue` 侧边栏 + 移动导航加 `RouterLink to="/agent"`（capability 条件渲染，`v-if`）。

**组件**（`components/agent/`）：

- `AgentMessageList.vue`：消息流容器。**a11y 决策**：容器 `role="log" aria-live="polite" aria-relevant="additions"`——只播报**新增气泡**，不播报既有气泡内的流式文本变化（`aria-relevant="additions"`）；细粒度事件（工具完成、审批到达、backend 状态变化、错误）由独立视觉隐藏 `role="status" aria-live="polite"` 区域以粗粒度播报（**绝不逐 chunk 播报**，屏幕阅读器噪音是不可接受的）。自动滚动仅在用户已贴底时执行（scroll anchoring），`prefers-reduced-motion` 下禁用平滑滚动。
- `AgentMessageBubble.vue`：assistant/user 气泡；文本**纯文本渲染**（`{{ }}` 插值，`white-space: pre-wrap`，**无 v-html、无链接化**）；流式气泡尾部带 `aria-hidden` 光标指示。
- `AgentToolActivity.vue`：工具活动行（工具名 + running/completed/failed 状态 + 可折叠 summary；summary 纯文本）。折叠状态用原生 `<button aria-expanded>`。
- `AgentComposer.vue`：`<textarea>` + 发送按钮；客户端校验镜像 `validate_plain_text`（非空、≤64 KiB、剔除控制字符除换行）；提交中 `aria-busy` 禁重复提交；202 后本地回显用户气泡 + 清空输入并**保持焦点**；运行中显示取消按钮（`POST /cancel`）；503/`binding_runtime_unavailable` → composer 置灰 + 提示（runtime 未就绪，fail-closed 语义）。
- `AgentApprovalPanel.vue`：会话内 pending 审批列表。数据源：挂载时 `GET /approvals?conversation_id=` 全量（replay 里的 CUSTOM 事件只保证卡片可见性，**状态以 REST 为准**，冷启动也必须拿到最新 pending 集），此后 decide/revoke 成功后刷新 + timeline 新 CUSTOM 到达时刷新（不引入轮询）；每条显示 `pane_id/operation/intent_summary`（可空降级：仅显示 canonical hash 摘要 + "详情不可用"）、`expires_at` 倒计时、approve/deny/revoke 按钮（原生 `<button>`，键盘可达）。**approve 需要确认弹窗**（高影响操作，复用焦点陷阱模式；deny/revoke 直接执行）。
- `AgentApprovalCard.vue`：timeline 内联只读卡（CUSTOM 事件驱动，懒加载 REST detail），"在审批面板处理"按钮聚焦面板对应条目。
- `AgentBackendStatus.vue`：状态徽标（connecting/ready/unavailable/context_lost/reconciling/closed → 中文标签），未知态"未知"。

**composables**：`useAgentConversation(conversationId)`（会话生命周期：冷启动/热恢复序列 §4.4、`AgentStreamSession` 接线、reducer 驱动 reactive 状态、`onAuthenticationRequired→clearSessionState+redirect /login?redirect=`（沿用 TerminalView 模式）、4412→`bindingRevoked` 状态 + toast + 清 cursor、4410→toast"连接恢复中"（session 内部自动 REST 恢复）、`onBeforeUnmount` dispose + AbortController）；`useAgentConversations()`（列表/创建/删除/审批角标）；`useAgentApprovals(conversationId?)`（列表 + decide/revoke + 错误映射：409 already_decided/revoked/consumed→刷新列表+toast；410 expired→刷新+toast；404→移除条目；409 auth_epoch_stale→提示重新登录）；`useAgentBroker()`（capability 门）。

### 4.8 提交与取消流（依赖 M4.5 端点）

- 提交：`createAgentsApi.submitMessage(conversationId, {text})` → 202 `AgentSubmitMessageResponse` → 本地回显 user 气泡（`deliveryState:'accepted'`）；此后 `RUN_STARTED`（live 帧）自然推进 UI；503/422/403/404 用 `ApiError`（既有 kind 文案）+ toast，回显保留文本待重试。
- 取消：运行中（`runs` 存在 active）显示取消按钮 → `POST /cancel` → 202 `{outcome, run_state}` → 刷新 run 状态；409 `no_active_run` → 静默刷新 run 状态（UI 已过期，非错误）。
- 用户消息历史对齐：`seedUserMessages` 仅在视图挂载时以 `/messages` REST（§6.6 body 依赖）注入一次，之后本会话回显直接追加（§4.5 时序分离论证）。

## 5. 数据与 API

- **消费的 B API**（全部既有，零新端点，除 §6.6 依赖）：
  - `GET /api/v1/agent/capabilities`；`GET /api/v1/agent/admin/bindings`；`GET/POST/DELETE /api/v1/agent/conversations[?binding_id=]`；`GET /api/v1/agent/conversations/{id}/messages`（§6.6 body）；`GET /api/v1/agent/conversations/{id}/events?wire=agui&since=&limit=`；`GET /api/v1/agent/stream?conversation_id=&cursor=&wire=agui`；`POST …/messages`、`POST …/cancel`（M4.5）；`GET /api/v1/agent/approvals[?conversation_id=]`、`GET /api/v1/agent/approvals/{id}`、`POST …/decide`、`POST …/revoke`。
- **client-core API 函数**：`api/agents.ts` 增 `submitMessage`、`cancelRun`、`fetchEventsSinceAgui`（内部按页循环：`?wire=agui&since=&limit=200`，`next_cursor` 前进才继续、空页/不前进即停；返回 `{events: AguiEvent[], coveredThrough: number}`——`coveredThrough` 为终止时 `next_cursor`，无事件时回传 `since`；批次内事件无 seq，水位归因交给 session，§4.3 问题二）；新 `api/approvals.ts`（`createApprovalsApi`：`list`/`detail`/`decide`/`revoke`）。
- **generated.ts 再生成**（本 spec 范围内）：`scripts/generate-client-contracts/generate.py` 增 `agent_approvals` 模型 import（`ApprovalResponse/ApprovalDetailResponse/ApprovalListResponse/ApprovalDecisionRequest` 等）；M4.5 的 submit/cancel 请求/响应模型随其端点落地后进入再生成；执行 `npm run contracts:check`。AG-UI 类型**不**进 generated.ts（手写 `agent/agui.ts`，M6a §4.7）。
- **cursor 存储**：localStorage key `termflow.agent.cursor.<conversationId>`，值 `{"cursor":"{epoch}-{seq}","seq":N}`；写前校验；会话删除/4412/404 清除。不含任何消息内容（opaque 游标，隐私契约测试覆盖）。
- **状态派生**：`backend` 由 STATE_DELTA 事件（replay 含历史值）驱动；`permissions` 可见性由 CUSTOM 事件，状态由 REST detail；无新客户端 schema 持久化（除 cursor）。

## 6. 安全

6.1 **认证复用，无新面**：所有流/REST 调用走既有 `createApiClient`（`credentials:'same-origin'`，相对路径）与 `require_admin` 会话；4401（流内）/401（REST，既有 `ApiError.kind='authentication'`）统一触发 `clearSessionState + redirect /login?redirect=`。无新 cookie、无 token 进 URL（cursor 是 opaque 游标非凭据）、无 CSRF 变化（GET 流只读；POST messages/decide/revoke 走既有同源 + admin session 门禁）。

6.2 **渲染安全**（计划 §13.2）：全部 agent 文本（消息、tool summary、evidence、error message、intent_summary）**纯文本插值渲染，禁用 v-html，不做链接自动识别**（`javascript:` URL 风险面整体消除）；渲染前剥离 ANSI/OSC 逃逸序列（`stripAnsiOsc()` 工具，client-core）；`TOOL_CALL_RESULT.content` 摘要 JSON 以文本呈现。隐私契约测试扩展：模拟含 `<script>`/ANSI/`javascript:` 的 delta，断言 DOM 无元素注入、storage/URL/console 无内容泄漏。

6.3 **cursor 隐私**：localStorage 只存 opaque cursor/seq，无消息内容；`privacy-contract.test.ts` 增断言。

6.4 **capability 门**：Agent 导航/路由由 `agent_broker_enabled` 控制（无认证端点，非敏感 flag）；disabled 时 Chat 不可达（视图占位），避免依赖 API 404。

6.5 **审批操作**：approve 走确认弹窗；decide/revoke 为 `require_admin` POST，409/410/404 按 §4.7 映射；决策按钮请求期间 `aria-busy` 防重复点击（B 侧 CAS 仍为最终防线）。

### 6.6 跨任务依赖：用户消息历史需要 `agent_messages.body`

刷新后用户消息文本在 B 无任何来源（`body_digest` only，事件流无 user message kind）——历史渲染缺用户侧内容，违反 M6 exit "chat, leave, return" 的完整历史语义。**本 spec 判定为阻断性前置缺口**，设计如下（严格镜像 M6a §4.2 payload 列先例，最小侵入）：

- `agent_messages` 增 `body TEXT NULL`（bounded ≤64 KiB，与 `MAX_AGENT_TEXT_BYTES` 一致）；写入时强制 `body_digest == sha256(body)` 不变量（`ValueError` fail fast）。
- `AgentMessageResponse` 增 `body: str | None`；M4.5 pipeline 在 user message enqueue/append 时写入 body（assistant 消息 body 由事件 delta 装配，M4.5 已定 body_digest 语义不变）。
- C 侧降级：`body === null`（迁移前历史行）渲染占位"历史消息内容不可用"；`seedUserMessages` 只消费 `role==='user'` 行（assistant 内容以事件 replay 为准，避免双源冲突）。

此项为**唯一** B 侧改动，是否并入 M6b 或拆为独立 B 任务由主 agent 决定（见开放问题 1）；spec 其余部分不依赖该决定（占位路径保证 UI 完整）。

## 7. 文件变更

新增：

- `packages/client-core/src/agent/agui.ts` —— AG-UI 类型 + `parseAguiEvent`
- `packages/client-core/src/agent/aguiFrames.ts` —— `parseAgentStreamFrameAgui`
- `packages/client-core/src/agent/history.ts` —— 纯 reducer 状态机
- `packages/client-core/src/agent/cursorStore.ts` —— `parseCursorSeq`/`isValidAgentCursor` + `AgentCursorStore` 持久化端口
- `packages/client-core/src/api/approvals.ts` —— `createApprovalsApi`
- `packages/client-core/src/agent/agui.test.ts`、`aguiFrames.test.ts`、`history.test.ts`、`cursorStore.test.ts`、`api/approvals.test.ts`
- `apps/clients/web/src/adapters/browserAgentStreamTransport.ts`（+ `.test.ts`，SSE 行缓冲/状态映射/abort）
- `apps/clients/web/src/adapters/browserAgentCursorStore.ts`（+ `.test.ts`，校验/roundtrip/非法丢弃）
- `packages/client-ui/src/views/AgentView.vue`、`AgentChatView.vue`（+ `.test.ts`）
- `packages/client-ui/src/components/agent/AgentMessageList.vue`、`AgentMessageBubble.vue`、`AgentToolActivity.vue`、`AgentComposer.vue`、`AgentApprovalPanel.vue`、`AgentApprovalCard.vue`、`AgentBackendStatus.vue`（+ 关键组件测试）
- `packages/client-ui/src/composables/useAgentBroker.ts`、`useAgentConversation.ts`、`useAgentConversations.ts`、`useAgentApprovals.ts`（+ `.test.ts`）
- `apps/clients/web/e2e/agent-chat.spec.ts` —— smoke（env 门控）

修改：

- `packages/client-core/src/agent/stream.ts` —— wire 泛化（cursor-seq 去重、批式水位 replay + live 缓冲、`seedFromSeq` 冷启动、agui 模式）
- `packages/client-core/src/agent/ports.ts` —— 传输事件负载泛型 `AgentStreamTransportEvent<TEvent>`；连接请求不变（wire 由 transport 构造时确定）
- `packages/client-core/src/agent/stream.test.ts` —— agui 用例扩展（§8）
- `packages/client-core/src/api/agents.ts` —— `submitMessage`/`cancelRun`/`fetchEventsSinceAgui`
- `packages/client-core/src/index.ts` —— 新导出
- `packages/client-ui/src/runtime.ts` —— `ClientRuntime` 增 `createAgentStream`/`agentCursorStore`
- `packages/client-ui/src/router/routes.ts` —— `/agent`、`/agent/:conversationId`
- `packages/client-ui/src/App.vue` —— capability 门控导航链接
- `packages/client-ui/src/index.ts` —— 导出
- `packages/client-ui/src/test/fakeRuntime.ts` —— 新端口桩
- `packages/client-ui/src/test/a11y-contract.test.ts` —— agent 断言扩展（§8）
- `packages/client-ui/src/styles/app.css` —— agent chat 样式（含 reduced-motion 规则）
- `apps/clients/web/src/runtime.ts` —— 接线新 adapter
- `apps/clients/web/src/test/privacy-contract.test.ts` —— agent 内容隐私断言
- `scripts/generate-client-contracts/generate.py` + `packages/client-contracts/src/generated.ts` —— 再生成（§5）

**不改**：B 的 `agent_stream.py`/`agent_conversations.py`/`agent_approvals.py`/`agent_capabilities.py`/投影模块（除 §6.6 依赖）、`frames.ts` canonical 解析器、`AgentStreamSession` 的恢复/闭流语义、`stream_hub.py`。

## 8. 测试矩阵

| 计划 1019 行验证项 | 测试位置与断言 |
|---|---|
| **offline replay**（Web） | client-core `stream.test.ts` + `history.test.ts`：冷启动序列——`seedFromSeq: 0` 无 cursor 订阅（live-only）+ 批式 replay 注入；live 帧在 seed 期间到达 → 缓冲后按 `coveredThrough` 水位丢弃/投放；最终 timeline = 完整有序集合、无 gap 无重复（水位收敛断言：缓冲 seq `<=` 水位丢弃、`>` 水位按序投放；seed 后 live 继续）。web adapter 测试：`fetchEventsSinceAgui` 分页（`next_cursor` 前进/空页终止、空结果 `coveredThrough=since`） |
| **cursor resume**（Web） | `stream.test.ts`：带 cursor 连接 → 服务器 replay 缺口帧后接 live；`cursorStore.test.ts`：save/load roundtrip、非法 cursor（非 `{epoch}-{seq}`）/负 seq → load 返回 null（冷启动）；4412/404/会话删除 → clear |
| **duplicate deltas**（Web） | `stream.test.ts`：同一事件经 REST 批次与 live 双路到达 → 仅投递一次（agui：批式水位 + 缓冲过滤丢弃 `seq <= coveredThrough`；canonical：cursor-seq 去重回归）；`history.test.ts`：重复 chunk 追加/END/RESULT/complete 后 chunk → 状态幂等 |
| **slow client recovery**（Web） | `stream.test.ts`：close 4410 → 批式 replay（`replayAgui`）从 `lastSeq` 推进水位 → 旧 cursor 重连 → 服务器重放区间被 `lastSeq` 丢弃 → 无丢失无重复（既有用例的 agui 版 + 批式水位断言） |
| **auth epoch closure**（Web） | `stream.test.ts`：close 4401 → `onAuthenticationRequired` 且不重连；client-ui `useAgentConversation` 测试：clearSessionState + redirect `/login?redirect=`；`browserAgentStreamTransport.test.ts`：初始 HTTP 401 → close 4401 |
| **binding revocation**（Web） | `stream.test.ts`：close 4412 → 终态不重连；transport 测试：403 → close 4412；`AgentChatView` 测试：banner + composer 置灰 + cursor 清除 |
| a11y 断言 | `a11y-contract.test.ts` 扩展：消息区 `role="log"` + `aria-relevant="additions"`；细粒度 `role="status"` 区存在；approve/deny/revoke 为原生 button（键盘可达，`tab` 顺序断言）；approve 确认弹窗焦点陷阱 + Escape 恢复焦点（沿用 ClosePaneDialog 模式）；`prefers-reduced-motion` 下滚动动画禁用（CSS 断言）；流式更新不改变 document.activeElement（composer 保焦） |
| 渲染安全 | client-ui 组件测试 + web `privacy-contract.test.ts` 扩展：`<script>`/HTML/ANSI-OSC/`javascript:` URL 文本 → 无元素注入、无 v-html、storage/URL/console 无内容 |
| 审批 UI 错误路径 | `useAgentApprovals.test.ts`：409 already_decided/revoked/consumed → 刷新 + toast；410 expired → 刷新 + toast；404 → 移除条目；409 auth_epoch_stale → 重新登录提示；decide 请求中 `aria-busy` |
| 提交/取消 | `useAgentConversation.test.ts`：202 → 用户气泡回显 + 焦点保持；422/503/403 → toast + 文本保留；cancel 202/409 路径；capability disabled → 视图占位、导航隐藏 |
| 契约再生成 | `npm run contracts:check`（generated.ts 含 Approval*/submit/cancel 类型）+ client-core/client-ui typecheck |

e2e（Playwright，`TERMFLOW_E2E_*` 门控，沿用 control-center.spec.ts 模式）：登录 → `/agent` 可见且无 console 错误 → 创建会话 → 无 runtime 时 composer 显示不可用；capability 关闭时导航隐藏、路由显示占位。深度聊天 e2e 依赖部署 fake backend，明确留出（§12 开放问题 4）。

## 9. 兼容

- 既有 canonical 客户端（`parseAgentStreamFrame`/`AgentStreamSession` canonical 模式/`fetchEventsSince`）行为不变；`wire=agui` 是纯增量。
- `AgentStreamSession` 泛化保持公共回调语义（`onStatus/onEvent/onReset/onClosed/onError/onAuthenticationRequired`）不变；canonical 模式追加 cursor-seq 交叉校验，若出现不一致按重复丢弃（安全方向）。
- 零新依赖（无 AG-UI npm 包——与 M6a"wire projection only、不引入包"原则一致，TS 类型手写）。
- localStorage cursor 对既有用户透明（新 key 前缀，不读不写其他 key）。

## 10. 风险与缓解

1. **AG-UI wire 漂移**（0.1.19 文档不一致、`TEXT_MESSAGE_CHUNK` 不在判别联合、未来 1.0 迁移——M6a 风险 2 的 C 侧镜像）：`parseAguiEvent` 按形状宽容校验、未知类型丢弃不崩；B 侧 pinned wire fixture（M6a）是契约锚点，漂移先被 B 测试暴露；若协议把 chunk 移出 wire，仅改 `agui.ts`/`history.ts` 两处。
2. **用户消息历史缺口**（§6.6 依赖未批准）：UI 全部路径以 `body: string | null` 设计，占位降级保证功能完整；风险明示于开放问题 1 待主 agent 决策。
3. **agui 全局流无 id 无法去重**：明确排除（§2/§4.3），Chat UI 仅 conversation-scoped；若未来需要，须先由 B 在 AG-UI 事件补稳定 id（跨任务，非本 spec）。
4. **fetch-stream 需手写 SSE 解析**：行缓冲 + `TextDecoder(stream:true)` 处理多字节跨 chunk、帧切分边界；单元测试覆盖分片/粘包/半帧/无效帧丢弃；`parseAgentStreamFrameAgui` 复用已测信封逻辑。
5. **后台标签页节流 → 4410 频发**：session 既有 4410→REST replay→重连收敛（每次恢复重放缺口，代价有限）；不引入 visibility 暂停策略（YAGNI，记录决策）；若实测抖动超限，后续可用既有 `VisibilityPort` 暂停订阅。
6. **M4.5 端点未落地时的前端依赖**：submit/cancel 组件以 M4.5 契约为准先行实现 + 假 API 测试，M4.5 落地后契约再生成对齐；若顺序颠倒（M6b 先于 M4.5），提交按钮临时禁用并显示"运行管线未就绪"（显式状态，非静默）。
7. **a11y 直播噪音**：`aria-relevant="additions"` + 粗粒度 status 播报（§4.7），流式 chunk 不触发播报；测试断言固定该策略防回归。
8. **cursor 持久化陈旧**（epoch 变更/会话删除/另一管理员操作）：服务器 `reset` 帧刷新、4412/404 清除、读取校验丢弃——持久化仅优化非正确性依赖（§4.4 论证）。

## 11. 决策摘要

- **transport：fetch-stream + ReadableStream**（§4.2），弃 EventSource——HTTP 状态码可见性（401/403/404 终态映射）是决定因素；transport 按 wire 构造（`createBrowserAgentStreamTransport({wire:'agui'})`）；`credentials:'same-origin'`、相对路径、无新认证面；重连/退避留在 `AgentStreamSession`。
- **去重 seq 取自信封 cursor**（`{epoch}-{seq}`），canonical 保留 `database_seq` 交叉校验；AG-UI TS 类型手写于 `agent/agui.ts`（不进 generated.ts），`parseAgentStreamFrameAgui` 与 canonical 解析器并存。
- **replay 改批式水位**（agui 事件无 seq、REST 页内 seq 不连续）：`replayAgui → {events, coveredThrough}` 一次推进 `lastSeq`，期间 live 帧缓冲、结束时按水位丢弃/投放；`reset`/4410 复用同一机制，4410 后旧 cursor 重连由 `lastSeq` 防重。
- **冷启动 = seedFromSeq 订阅 + 批式 REST replay（session 内缓冲 merge）**；热恢复 = 持久化 cursor 直接订阅；cursor 存 localStorage（端口 `AgentCursorStore`，正确性不依赖）。
- **客户端状态机**：client-core 纯 reducer `history.ts`（messages/toolCalls/runs/permissions/backend/timeline/userMessages，幂等应用 AG-UI 事件；审批状态 B REST 为 source of truth；backend 状态由 STATE_DELTA 驱动、capabilities REST 作 UI 门）。
- **UI**：`/agent` 总览 + `/agent/:conversationId` 聊天；capability 门控导航与路由；纯文本渲染（无 v-html、无链接化、ANSI/OSC 剥离）；消息流 `role="log" aria-relevant="additions"` + 粗粒度 `role="status"` 播报；approve 需确认弹窗（焦点陷阱沿用既有模式）。
- **审批错误映射**：409 各子码 → 刷新+toast；410 → 刷新+toast；404 → 移除；`auth_epoch_stale` → 重新登录提示。
- **端口扩展**：`ClientRuntime` 增 `createAgentStream` + `agentCursorStore`（Tauri 复用同一 UI）。
- **契约再生成**：generate.py 增 agent_approvals（+M4.5 模型随端点落地）→ `npm run contracts:check`。
- **唯一 B 侧依赖**：`agent_messages.body` bounded 可空列 + `/messages` 暴露（镜像 M6a payload 先例），历史用户消息渲染的阻断性前置（§6.6，开放问题 1）。

## 12. 开放问题

1. **§6.6 `agent_messages.body` 依赖归属**：并入 M6b（含迁移 0010 + pipeline 写入点）或拆为独立 B 任务先行？spec 推荐独立小任务（与 M6a §4.2 同型），M6b UI 以 `body: string | null` 降级实现不受阻。
2. **M4.5 与 M6b 落地顺序**：submit/cancel 端点（M4.5）是 composer 的硬依赖；若 M6b 先行，提交按钮以"管线未就绪"禁用态交付（§10 风险 6）。
3. **agui 全局流**：本 spec 排除（无 id 去重）；M6a §4.4 的"按 threadId 归并"是否需要后续 B 侧补稳定 id 后支持，留待主 agent 确认。
4. **e2e 深度**：Playwright 只做 smoke（capability 门 + 路由 + 无 runtime 状态）；带 fake backend 的完整聊天 e2e 依赖部署夹具，是否需要本 release 内补齐。
5. **会话删除时的 cursor 清理**：DELETE 成功后由客户端清除 localStorage（无服务器通知）；另一标签页的陈旧 cursor 由 404→4412 终态覆盖——是否需要跨标签页同步（`storage` 事件）暂不做（YAGNI）。
