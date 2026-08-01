# TermFlow V1 设计规格

- 日期：2026-08-01
- 状态：已批准进入实施计划
- 项目：TermFlow

## 1. 产品定义

TermFlow 是一个本地优先的远程终端控制器。用户在电脑上执行
`termflow new`，得到一个具备完整 tmux 交互能力的隔离终端实例；该实例
自动向中央 Control Plane 注册。未来的手机、网页或桌面客户端只连接
Control Plane，即可查看并操控已经存在的 Pane。

V1 交付 A 与 B：

- A：安装在 Linux、macOS 或 WSL 上的 TermFlow Node 应用；
- B：单实例部署的 TermFlow Control Plane；
- C：只保留目录和协议边界，不实现客户端。

TermFlow V1 不是 Task Manager、任务调度器、AI Agent 平台或终端录制平台。
其核心价值是让本地 tmux 中的交互式程序在用户 detach 后继续运行，并通过
受认证的中央中继接收远程普通文本输入和发送实时输出。

## 2. 术语

### 2.1 Installation

一台电脑上的一次 TermFlow 登录关系。Installation Credential 只允许创建新
Instance，不能读取或控制其他 Instance。

### 2.2 Instance

一次 `termflow new` 创建的独立运行单元，也是早期讨论中的 A。一个 Instance
拥有独立的：

- UUID；
- Instance Credential；
- tmux server 和私有 socket；
- 一个受 TermFlow 管理的 tmux Session；
- Bridge 后台进程；
- 到 B 的 WSS 连接；
- 生命周期和故障边界。

同一台电脑可以同时运行多个 Instance。Instance 不是物理电脑的同义词。

### 2.3 Window 与 Pane

沿用 tmux 的 Window 和 Pane 语义。一个 Instance 只创建一个受管理的 Session，
Session 内可包含任意正常数量的 Window 和 Pane。需要另一个 Session 时，用户
再次执行 `termflow new`，从而创建另一个独立 Instance。

### 2.4 Bridge

每个 Instance 的后台桥接进程。Bridge 作为 tmux control mode client 观察本地
拓扑与输出，同时主动连接 B，负责协议转换、重连、内存缓冲和命令确认。

### 2.5 Control Plane

B 端 FastAPI 服务。它负责认证、注册、在线状态、单目标路由、实时中继和不含
正文的审计，不运行 tmux，不访问用户项目目录，不保存终端正文。

## 3. V1 范围

### 3.1 V1 必须支持

- 一次安全登录后，每次 `termflow new` 自动注册一个独立 Instance；
- 本地使用真正的 tmux client，获得完整分屏、布局、快捷键、鼠标、detach 和
  attach 能力；
- 多个 Instance 使用独立 tmux socket、Bridge、身份和 WSS；
- B 列出在线 Instance 及其真实 Window/Pane 拓扑；
- 通过 B 向一个已经存在的 Pane 输入普通文本，并显式选择是否附加 Enter；
- 通过 B 实时订阅 Pane 的原始输出；
- Bridge 与 B 的网络断开时，本地 tmux 继续运行；
- B 恢复后 Bridge 自动重连并重新同步拓扑；
- A 离线时 B 立即拒绝远程输入，不持久排队；
- B 不持久化输入文本或终端输出。

### 3.2 V1 明确不支持

- 手机、网页或桌面客户端实现；
- 远程创建、拆分、关闭 Window 或 Pane；
- Ctrl+C、Escape、方向键或其他特殊键；
- 远程选择 Profile、Workspace 或绝对路径；
- Codex、Claude Code 或其他 Agent 的专用适配器与语义状态；
- B 端 AI Agent、STT、TTS 或自然语言意图解析；
- Kafka、Redis、NATS 或其他外部消息系统；
- 多 B 实例和多 Uvicorn worker；
- PostgreSQL；
- 终端录制、正文审计或正文日志；
- 原生 Windows ConPTY；
- 电脑重启或 tmux server 退出后的会话恢复。

Ctrl+C 等能力可在后续版本通过受约束的特殊键协议增加。未来的 B 端 AI Agent
必须作为普通的受认证 API 调用方工作，不能绕过相同的路由、确认和审计边界。

## 4. 方案选择

V1 采用“每个 Instance 独立 tmux server + 独立 Bridge + 独立 WSS”。

未选择的方案：

1. 每台电脑一个 Supervisor、多个 tmux server 和一条复用 WSS：资源较少，但
   Supervisor 成为同一电脑所有 Instance 的共同故障域，路由也更复杂。
2. 所有 Instance 共用一个 tmux server，以 Session 区分：资源最少，但共享
   socket、生命周期和权限，不符合 Instance 独立性。

选择独立方案的代价是每个 Instance 多一个轻量 Bridge 进程和 WSS；收益是身份、
本地状态、网络连接和关闭行为都具有明确边界。

## 5. 总体架构

```text
电脑本地
┌──────────────────── TermFlow Instance ──────────────────────┐
│                                                             │
│ 本地终端 ── tmux client ── 私有 tmux server/socket           │
│                                  │                          │
│                                  └─ Session/Window/Panes    │
│                                  │                          │
│                    TermFlow Bridge (control mode client)    │
└──────────────────────────────────┼──────────────────────────┘
                                   │ 主动建立、自动重连的 WSS
                                   ▼
                         ┌──────────────────┐
                         │ B: Control Plane │
                         │ 认证、注册、路由  │
                         │ 在线状态、审计    │
                         └────────┬─────────┘
                                  │ HTTP/WSS
                                  ▼
                          C: 未来控制客户端
```

所有到 A 的网络连接均由 Bridge 主动发起。A 不监听公网端口。B 是唯一公开入口，
因此可在 NAT、家庭网络和公司网络后使用。

## 6. Monorepo 与组件边界

```text
TermFlow/
├── pyproject.toml
├── uv.lock
├── README.md
├── apps/
│   ├── node/
│   │   ├── pyproject.toml
│   │   ├── src/termflow_node/
│   │   │   ├── cli/
│   │   │   ├── config/
│   │   │   ├── instances/
│   │   │   ├── tmux/
│   │   │   └── bridge/
│   │   └── tests/
│   ├── control-plane/
│   │   ├── pyproject.toml
│   │   ├── src/termflow_control_plane/
│   │   │   ├── api/
│   │   │   ├── auth/
│   │   │   ├── connections/
│   │   │   ├── routing/
│   │   │   ├── persistence/
│   │   │   └── audit/
│   │   └── tests/
│   └── clients/
│       └── README.md
├── packages/
│   └── protocol/
│       ├── pyproject.toml
│       ├── src/termflow_protocol/
│       └── tests/
├── deploy/
│   ├── Dockerfile.control-plane
│   └── compose.yaml
└── docs/
    ├── architecture.md
    ├── protocol.md
    ├── security.md
    └── api-examples.md
```

Python 版本为 3.12，使用 uv workspace 管理依赖和锁文件。A 使用 asyncio、tmux
与 WebSocket；B 使用 FastAPI、Pydantic 和 SQLite。共享 protocol 包只包含版本化
线协议模型，不引用 A 或 B 的业务实现。

### 6.1 Node 应用

用户只安装并调用一个 `termflow` 命令。内部包含：

- CLI：`login`、`new`、`list`、`attach`、`kill`、`status`、`doctor`；
- Instance Manager：创建本地状态、进程、tmux socket 和凭据；
- tmux Adapter：调用 tmux 命令并解析 control mode；
- Bridge：连接 B、发送心跳、同步拓扑、传输输出、接收普通文本输入；
- Config：保存 Installation Credential、B 地址和本地 Instance 元数据。

TermFlow 使用独立命名的 tmux socket，不访问用户普通 tmux server。V1 要求 tmux
3.2 或更高版本。专属 server 仍加载当前用户的标准 tmux 配置，以保留用户熟悉的
快捷键和界面；TermFlow 只设置协议工作所需的实例标识和 control mode 选项。

### 6.2 Control Plane

B 是一个 FastAPI 进程和一个 Uvicorn worker，包含：

- 管理和控制 REST API；
- Bridge WSS 接入点；
- 未来 C 使用的事件 WSS；
- 注册码、Installation 和 Instance 认证；
- `instance_id -> live connection` 内存注册表；
- 单目标命令路由、确认与超时；
- SQLite repositories；
- 不含正文的结构化审计。

### 6.3 Clients 占位

`apps/clients/README.md` 只记录 C 的边界和后续兼容要求。V1 不包含网页、手机、
桌面 GUI 或终端渲染组件。

## 7. 本地生命周期

### 7.1 首次登录

B 的受认证管理员通过本机 CLI 或管理 API 创建一次性注册码：

```bash
termflow-control enrollment create
```

或：

```http
POST /api/v1/enrollment-tokens
Authorization: Bearer <admin-token>
```

用户在电脑上执行：

```bash
termflow login \
  --server https://termflow.example.com \
  --enrollment-token <single-use-token>
```

注册码由高熵随机数据生成，十分钟过期，只能使用一次。B 只保存其哈希。交换成功
后，A 保存 Installation Credential；该凭据只能注册 Instance。

未来 C 可以在用户完成认证后调用同一个注册码 API，并以文字或二维码展示，但这
不属于 V1。

### 7.2 创建 Instance

```bash
termflow new --name project-a
```

顺序如下：

1. 读取 Installation 配置；
2. 在本地生成 Instance UUID 和私有状态目录；
3. 创建专属 tmux socket、server 和一个受管理 Session；
4. 启动 Bridge 后台进程；
5. 当前终端以普通 tmux client 附着；
6. Bridge 使用 Installation Credential 向 B 注册；
7. B 签发独立 Instance Credential；
8. Bridge 切换为 Instance Credential 并保持 WSS。

B 暂时不可用时，第 2 至 5 步仍然成功。Bridge 在后台重试第 6 步，本地用户不会
被 B 的可用性阻塞。

### 7.3 Attach、detach 与 kill

- tmux detach 只退出当前本地 client；tmux server、Pane 和 Bridge 继续运行；
- `termflow attach <instance>` 检查 Bridge 状态并附着已有 Session；
- `termflow list` 展示本机 Instance 与 tmux/Bridge 存活状态；
- `termflow doctor` 检测依赖、socket、凭据和 Bridge，并可重新启动缺失的 Bridge；
- `termflow kill <instance>` 终止 Bridge 和对应 tmux server；若 B 在线则标记关闭，
  若 B 离线则 B 最终通过心跳超时将其标记为离线。

电脑重启或 tmux server 退出后，V1 不恢复原会话。Bridge 退出不会主动终止 tmux；
重新执行 attach 或 doctor 可以为仍存活的 tmux server 重建 Bridge。

## 8. 协议

### 8.1 通用 Envelope

所有 JSON 控制消息使用版本化 Envelope：

```json
{
  "protocol_version": 1,
  "message_id": "uuid",
  "type": "topology.snapshot",
  "instance_id": "uuid",
  "sent_at": "2026-08-01T00:00:00Z",
  "payload": {}
}
```

未知主版本必须被明确拒绝，不能静默降级。凭据通过 Authorization header 或 WSS
握手 header 传输，不放在 URL query 或消息正文中。

### 8.2 Bridge 到 B

主要消息：

- `bridge.hello`：版本、Instance 身份和能力；
- `bridge.heartbeat`：在线心跳；
- `topology.snapshot`：完整 Session/Window/Pane 快照；
- `topology.changed`：本地 tmux 产生的增量变化；
- `pane.output`：带流标识和序号的原始输出；
- `command.result`：输入命令的成功或失败结果。

### 8.3 B 到 Bridge

V1 只有两类：

- `pane.input`：向一个已有 Pane 写普通文本，可选附加 Enter；
- `pane.replay_request`：请求从某个 `stream_id + seq` 补发输出。

不提供原始 tmux 命令、Pane 生命周期命令或特殊键通道。

## 9. HTTP 与事件 API

### 9.1 V1 控制接口

```text
GET  /api/v1/instances
GET  /api/v1/instances/{instance_id}
GET  /api/v1/instances/{instance_id}/topology
POST /api/v1/instances/{instance_id}/panes/{pane_id}/input
WS   /api/v1/events
```

Instance 列表来自持久身份记录和当前在线状态。拓扑只在 Instance 在线时返回；B
不把离线前的内存快照伪装为当前状态。

事件 WSS 要求一个 `instance_id` 过滤条件。需要补取输出时，调用方同时提供
`pane_id`、`stream_id` 和 `after_seq` 三个游标字段；B 在订阅建立后向在线 Bridge
发送一次 `pane.replay_request`。事件 WSS 不接受客户端主动发送命令。

输入请求：

```http
POST /api/v1/instances/{instance_id}/panes/{pane_id}/input
Authorization: Bearer <admin-token>
Idempotency-Key: <uuid>
Content-Type: application/json
```

```json
{
  "text": "继续",
  "submit": true
}
```

`text` 必须是合法 Unicode 普通文本，拒绝 ESC、NUL 和其他控制字符；`submit`
显式决定是否追加 Enter。这样 V1 不会通过文本接口变相提供 Ctrl+C 或终端转义。
单次输入默认最大 16 KiB，可由 B 和 A 同时限制，A 采用更严格的一方。

### 9.2 单发与并发

- 每个请求只能指定一个 Instance 和一个 Pane；
- 不提供广播或批量输入端点；
- 同一 Pane 的远程输入按 Bridge 接收顺序串行处理；
- 不同 Pane 和不同 Instance 可以并发；
- 本地 tmux client 与远程输入都属于可信控制方，tmux 按实际到达顺序处理；
- B 为每条命令分配 `command_id`，只有收到 A 的确认后才向 HTTP 调用方返回成功；
- A 离线时立即拒绝，不把命令存入数据库。

### 9.3 幂等

输入端点要求调用方发送 UUID 格式的 `Idempotency-Key`。Bridge 在有界内存中保存
近期命令结果。相同 key 重试时返回先前结果，不再次写入 Pane。若命令已经发送但
确认丢失，API 返回 `outcome_unknown`；调用方可使用相同 key 重新提交同一请求，
由 Bridge 返回已缓存的实际结果而不重复写入。

## 10. tmux 拓扑与实时输出

Bridge 使用 tmux control mode 接收：

- Window/Pane 创建、选择、重命名、尺寸变化和退出事件；
- 所有 Pane 的原始输出；
- 命令执行结果。

协议使用 tmux 的内部 Session、Window 和 Pane ID，不以可变化的显示索引作为
身份。Pane 的全局目标由 `instance_id + pane_id` 唯一确定。

tmux 输出可能不是 UTF-8，也可能含终端转义序列，因此 `pane.output` 用 Base64
传输原始字节。每个 Pane 的事件包含：

- `stream_id`：Bridge 当前输出流的 UUID；
- `seq`：该流中递增的序号；
- `data_base64`：原始输出；
- `captured_at`：采集时间。

Bridge 为每个 Pane 维护默认 1 MiB 的内存环形缓冲。客户端重连时提供最后收到的
`stream_id + seq`：

- 数据仍在缓冲时，Bridge 补发缺失段；
- 数据已覆盖或 Bridge 已重启时，发送 `stream_gap`，随后通过 `capture-pane`
  提供当前屏幕快照，再开始新流；
- B 只转发，不落盘、不写数据库、不把内容写入应用日志。

## 11. 连接、重连和在线状态

每个 Instance 维护一条主动建立的 WSS。默认行为：

- 心跳间隔 15 秒；
- B 在 45 秒未收到有效心跳后标记 Instance 离线；
- Bridge 以 1 秒起步、上限 30 秒的指数退避加随机抖动重连；
- B 重启后所有 Bridge 自行重连；
- 重连成功后 Bridge 首先发送完整拓扑快照；
- 断线期间本地 tmux、Pane 和输入完全正常；
- B 不保存也不延迟执行断线期间收到的远程输入。

这些值可配置，但 V1 文档和测试使用上述默认值。

## 12. 背压与资源限制

B 和 Bridge 都必须使用有容量限制的异步队列。Kafka 不参与 V1。

- 同一 Pane 的命令队列保持 FIFO；
- 每个 WSS 连接拥有独立发送队列；
- Bridge 优先保证本地 tmux，不因远程慢消费者阻塞 Pane；
- 输出缓冲满后覆盖最旧数据，并通过 `stream_gap` 显式告知；
- B 面向事件订阅者的队列满时断开慢订阅者，不反向阻塞 Bridge；
- control mode 使用 tmux 的流控能力，落后时可暂停输出通知并在恢复后用
  `capture-pane` 重建当前视图；
- 输入大小、队列长度、缓冲字节数、确认超时和心跳均有配置上限。

## 13. 安全设计

### 13.1 凭据

V1 使用：

| 凭据 | 权限 |
| --- | --- |
| Admin Token | 管理 B、生成注册码、查看和控制所有 Instance |
| Installation Credential | 只注册新 Instance |
| Instance Credential | 只连接并上报该 Instance |

Admin Token 通过部署 secret 或环境变量注入，不写入仓库。其他 token 使用至少
256 位随机值，B 只保存哈希，并以恒定时间方式比较。Instance Credential 可以
单独吊销，不影响同一电脑上的其他 Instance。

### 13.2 本机边界

- 配置和凭据文件权限为 `0600`；
- Instance 状态目录和 tmux socket 目录权限为 `0700`；
- 每个 Instance 使用不可预测且独立的 socket 路径；
- Bridge 与本地 tmux 以同一 OS 用户运行；
- 能访问 tmux socket 的本地进程视为完全可信，TermFlow 不把 tmux socket 当成
  安全沙箱；
- B 永远不能直接访问该 socket，只能发送 V1 协议允许的输入消息。

### 13.3 网络与日志

- 生产只允许 HTTPS/WSS；
- HTTP/WS 只允许绑定 loopback 的开发模式；
- token 不放入 URL；
- 日志统一脱敏，不输出凭据、输入文本、终端输出或 Base64 数据；
- 审计只记录用户、Instance、Pane、操作类型、输入字节数、时间和结果。

由于向 shell Pane 输入文字本质上可执行命令，远程控制权限等同于对相应 tmux
会话的操作权限。V1 的安全边界依赖强认证、TLS、最小凭据权限和私有本地 socket，
不宣称对已认证控制方进行命令沙箱隔离。

## 14. 持久化

B 使用 SQLite WAL，持久化：

- Installation 身份与凭据哈希；
- Instance UUID、名称、凭据哈希、创建时间和生命周期状态；
- 一次性注册码哈希、过期时间和使用状态；
- 不含正文的审计元数据。

B 不持久化：

- 输入文本；
- Pane 输出；
- 终端屏幕快照；
- 断线待执行命令；
- 作为当前事实的离线 Pane 拓扑。

在线连接和当前拓扑快照只存在于 B 进程内存。数据访问代码通过 repository 边界
隔离 SQLite，但 V1 不实现 PostgreSQL。

## 15. 错误语义

统一错误格式：

```json
{
  "error": {
    "code": "instance_offline",
    "message": "The instance is not connected.",
    "request_id": "uuid"
  }
}
```

V1 至少定义：

- `unauthorized`；
- `forbidden`；
- `instance_not_found`；
- `instance_offline`；
- `pane_not_found`；
- `invalid_input`；
- `payload_too_large`；
- `backpressure`；
- `command_timeout`；
- `connection_lost`；
- `outcome_unknown`；
- `protocol_version_unsupported`。

HTTP 状态码与错误 code 建立固定映射。错误日志只记录 ID 和元数据，不记录正文。

## 16. 测试策略

### 16.1 单元测试

- Pydantic 协议模型和版本拒绝；
- tmux control mode 消息解析与字节解码；
- 输出环形缓冲、`stream_id`、序号和 gap；
- 同 Pane 串行与跨 Pane 并发；
- Token 哈希、注册码单次使用与过期；
- Idempotency-Key 去重和结果缓存；
- 输入控制字符与大小限制；
- 统一错误映射；
- 日志和审计不包含终端正文。

### 16.2 集成测试

CI 安装真实 tmux，并对每个测试使用临时 socket 和目录：

- 创建专属 tmux server、Session、Window 和 Pane；
- 本地拆分或关闭 Pane 后得到正确拓扑；
- control mode 接收真实 Pane 输出；
- B 输入普通文本后到达指定 Pane；
- detach 后 tmux 和 Bridge 继续运行；
- B 断开时本地 Pane 不受影响；
- B 恢复后 Bridge 自动重连并重新同步；
- A 离线时 B 立即拒绝；
- 多个 Instance 的 socket、身份、连接和输出不会串线。

### 16.3 端到端测试

使用真实 B、Bridge、tmux 和临时 SQLite 验证：

```text
生成注册码
→ termflow login
→ termflow new
→ B 显示在线 Instance
→ 本地拆分 Pane
→ B 获取最新拓扑
→ REST 向一个 Pane 输入普通文本
→ 事件 WSS 收到对应输出
→ 本地 detach
→ 远程链路仍有效
→ 重启 B
→ Bridge 重连且本地 Pane 未退出
```

测试不访问用户现有 tmux socket、配置状态或持久数据库。

## 17. 部署与文档

### 17.1 A

- 以 Python CLI 包发布；
- 支持 Linux、macOS、WSL；
- 安装或启动时检查 Python 3.12、tmux 3.2+；
- 使用平台标准配置、状态和运行目录；
- 提供 login/new/list/attach/kill/status/doctor 使用说明。

### 17.2 B

- 提供 Control Plane Dockerfile 和 Compose；
- SQLite 使用持久化 volume；
- 开发环境支持 uv 直接启动；
- 生产文档要求 TLS 反向代理和 secret 注入；
- 明确只运行一个 Uvicorn worker。

### 17.3 文档

- 根 README：产品定位、快速开始和 V1 边界；
- architecture：A/B/C、Instance、tmux 与 Bridge；
- protocol：HTTP/WSS 模型、序号、错误和版本；
- security：权限等价性、TLS、token、本地 socket 和隐私；
- api-examples：curl 和 Python 调用示例；
- troubleshooting：tmux 依赖、Bridge 重连、B 离线和 doctor。

## 18. V1 验收标准

只有以下证据全部成立，V1 才算完成：

1. `termflow new` 创建独立 tmux Instance 并进入完整 tmux 界面；
2. 同一电脑可运行多个互相隔离的 Instance；
3. 本地 detach 后 tmux 和 Bridge 继续运行；
4. B 能列出在线 Instance 和真实 Window/Pane 拓扑；
5. B 能向一个已有 Pane 输入普通文本，并在 A 确认后返回成功；
6. 事件 WSS 能实时转发指定 Pane 的原始输出；
7. B 断联不影响本地运行，恢复后 Bridge 自动重连；
8. A 离线时 B 立即拒绝且不排队；
9. B 的数据库、审计和日志不含输入或输出正文；
10. 单元、真实 tmux 集成和端到端测试通过；
11. Docker 部署、安装、API 示例和安全文档齐全。
