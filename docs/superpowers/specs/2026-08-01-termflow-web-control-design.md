# TermFlow Web 控制客户端与完整 tmux 远程控制设计

- 日期：2026-08-01
- 状态：已批准，等待实施计划
- 范围：扩展现有 A+B，并交付由 B 同镜像托管的 Web C
- 前置实现：提交 `4bffc36` 的 TermFlow V1 A+B

## 1. 目标

本阶段把现有的 HTTP/Pane 输出能力扩展为可实际使用的远程 tmux 控制产品：

- B 提供控制中心、电脑注册和中继能力；
- Web C 是独立客户端，只通过公开 HTTP/WebSocket 契约使用 B；
- Web C 与 B 构建进同一个 Docker 镜像并由同一地址提供；
- 用户可以从电脑或手机看到并操控 A 上真实的完整 tmux 客户端；
- A 上的 tmux Session 在 B 或 C 断开后继续运行；
- B 不持久化终端输入正文、输出正文或终端录像。

Web C 在产品角色上属于 C，而不是 B 的内部页面。与 B 同容器部署只是降低首版部署
成本，不能破坏客户端与控制平面的协议边界。

## 2. 产品模型与术语

层级固定为：

```text
Computer（一次 termflow login）
└── Term（一次 termflow new，一个独立 tmux server/session）
    ├── Window
    └── Pane
```

### 2.1 Computer

Computer 对应服务端内部的 Installation。A 首次注册时自动上报：

- hostname；
- 操作系统；
- TermFlow 客户端版本；
- 首次注册时间和最近在线时间。

B 保存一个可编辑的显示名称。显示名称与 hostname 分离，避免用户改名后丢失机器的
原始标识。

### 2.2 Term

Term 对应现有 Instance，也对应一个独立 tmux server 中唯一受管理的 Session。
一台 Computer 可以拥有多个 Term。

Term 的名称以 A 上真实的 tmux Session name 为唯一真相：

- `termflow new --name NAME` 使用 NAME 创建 tmux Session；
- Web C 重命名 Term 时，A 执行真实的 tmux `rename-session`；
- 用户在 A 的 tmux 中重命名后，A 通过拓扑更新同步给 B；
- B 持久化最后一次看到的 Session name，供离线列表使用；
- A 的本地 Instance 元数据同步更新，确保 `termflow list/attach` 使用最新名称。

A 的控制命令始终以稳定的 tmux Session ID（例如 `$0`）为目标，不能把显示名称拼入
tmux 命令字符串。因此本地或网页重命名不会破坏 attach、Bridge 或路由。名称限制为
1 至 128 个不含控制字符的 Unicode 字符，并作为独立 argv 参数传给 tmux。

现有 V1 本地记录升级时，A 读取唯一 Session 的实际 ID/name：如果旧记录仍是默认
`main` 且未被本地改名，则一次性改为旧 Instance display name；如果用户已经在 tmux
中改名，以实际 tmux name 为准。迁移后本地记录保存稳定 Session ID 和最新显示名称。

Term 内不识别任何 AI Agent 品牌。列表需要运行状态时，只展示 tmux 提供的原始字段，
例如 `pane_current_command`、Pane title、Window name 和是否为活动 Pane。生产 UI 不包含
Codex、Claude Code 等硬编码判断或状态映射。

## 3. 总体架构

```text
┌──────────────────────── B Docker image ───────────────────────┐
│                                                               │
│  Web C SPA                       B Control Plane               │
│  apps/clients/web                apps/control-plane            │
│  xterm.js + responsive UI  ⇄    HTTP/WS、认证、注册、路由      │
│        只使用公开 /api/v1 与 WebSocket 契约                    │
└───────────────────────────────┬───────────────────────────────┘
                                │ 已有主动 Bridge WSS，多路复用
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
        Computer A1                    Computer A2
        ├─ Term 1                      ├─ Term 1
        └─ Term 2                      └─ Term 2
           └─ tmux server/session         └─ tmux server/session
              └─ remote tmux client PTY      └─ remote tmux client PTY
```

边界规则：

1. Web C 不读 B 的数据库，不导入 B 的 Python 模块。
2. B 不解释终端正文，不运行 tmux，不访问 A 的项目目录。
3. A 不开放入站端口；每个 Term 的 Bridge 继续主动连接 B。
4. 共享协议保持版本化和消息系统中立，不暴露 Kafka/NATS 概念。
5. 未来 App、EXE 和原生客户端与 Web C 平级，复用同一契约。

## 4. 为什么使用真实 tmux client PTY

现有 V1 的 `pane.output` 与 `pane.input` 适合自动化 API，但不足以提供完整 tmux
控制台：把按键直接写入 Pane 无法让 `Ctrl+B`、Window 切换、copy mode、zoom 等
tmux client 级操作生效，也无法自然展示完整分屏和状态栏。

完整终端通道采用真实 tmux client PTY：

1. Web C 为一个 Term 打开终端 WebSocket；
2. B 在对应 Bridge 上创建 `terminal.open` 请求；
3. A 创建 PTY，并在 PTY slave 上运行连接既有私有 socket/session 的 tmux client；
4. PTY master 的原始输出经 A → B → C 传输；
5. C 的所有可交付键盘、IME、粘贴和鼠标终端输入按原始字节反向写入 PTY；
6. 关闭远程 PTY client 只 detach 该 client，不终止 tmux server/session。

这样 C 看到的是 tmux 自己绘制的 Window、Pane 边框、状态栏、copy mode 和用户 tmux
配置，而不是 C 根据 Pane 快照仿制的界面。

现有普通文本 HTTP 输入与单 Pane 事件 API继续保留，用于 curl、自动化和未来 B 端
Agent；完整 Web 终端走新的独立通道。

## 5. A 权威终端尺寸

A 的字符网格尺寸是唯一真相，C 永远不能改变 A 的尺寸。

A 的尺寸选择规则：

1. 从 tmux client 列表中排除 TermFlow 创建的远程代理 client；
2. 选择最近活动的本地 A client 的 rows/cols；
3. 没有本地 attached client 时使用最后一次观察到的 A 尺寸；
4. 从未观察到尺寸时使用 tmux 创建时的尺寸，最终兜底为 80×24；
5. A 本地尺寸改变时同步调整远程代理 PTY，并发送 `terminal.size` 事件。

C 用 A 提供的 rows/cols 初始化或更新 xterm.js，但不向 A 发送 resize：

- 桌面端：显示设置提供 50%、75%、100%（实际字号）和适应窗口；
- 手机端：支持双指缩放、单指拖动和聚焦当前 Pane；
- 聚焦 Pane 只是 C 端裁切/放大，不执行 tmux zoom；
- 真实 tmux zoom 是明确标注的辅助动作，会影响 A 上的 Session；
- 横竖屏旋转只重算 C 的 viewport 和缩放倍率。

## 6. 完整终端协议

### 6.1 B-C WebSocket

端点按 Term 限定：

```text
WS /api/v1/terms/{instance_id}/terminal
```

握手使用浏览器会话 Cookie。文本帧承载控制事件，二进制帧承载终端数据：

- B → C 二进制：PTY 输出；
- C → B 二进制：终端输入；
- `terminal.ready`：terminal_id、rows、cols、stream_id；
- `terminal.size`：A 权威尺寸变化；
- `terminal.binding_snapshot`：Prefix 与辅助动作快捷键说明；
- `terminal.error`：结构化错误；
- `terminal.closed`：关闭原因。

单个二进制帧限制为 64 KiB；大块粘贴与输出必须分块。B 对连接、输入速率和队列
大小设置明确上限。超限时返回结构化错误或关闭该终端连接，不能拖慢 A 的 Bridge。

### 6.2 A-B Bridge 多路复用

现有 Bridge WSS 增加：

- `terminal.open` / `terminal.opened`；
- `terminal.input`；
- `terminal.output`；
- `terminal.size`；
- `terminal.bindings`；
- `terminal.action` / `terminal.action_result`；
- `terminal.close` / `terminal.closed`。

消息以 `terminal_id` 区分并发通道。A-B 继续使用版本化 JSON envelope，原始字节在
payload 中使用严格 Base64；B-C 的边缘 WebSocket 转换成二进制帧，避免浏览器端 JSON
和 Base64 开销泄漏进终端组件。

每个 Term V1 只允许一个可输入的远程 tmux client。新的已认证连接替换旧连接；旧
连接收到 `terminal.closed(reason="replaced")`。这避免手机与桌面同时向同一个 tmux
client 键入。以后可增加只读观察者，不在本阶段实现。

### 6.3 重连

A 为远程 PTY 通道保留 30 秒断线宽限和最多 1 MiB 的内存输出环形缓冲：

- C 在宽限期内以 terminal_id、stream_id 和最后序号恢复时，A 补发缺失输出；
- 缓冲出现 gap 或宽限期结束时，旧代理 client 被关闭；
- B 创建新的 tmux client PTY，tmux 自然完成一次全屏重绘；
- C 在新 stream 开始前清空本地 xterm 状态；
- B 不把缓冲写入 SQLite、日志或审计表。

本地 tmux server/session 不受远程通道生命周期影响。

### 6.4 拓扑扩展

共享 `PaneSnapshot` 在保持现有字段兼容的基础上增加：

- `left`、`top`：以 tmux cell 为单位的 Pane 左上角坐标；
- `current_command`：tmux 原始 `pane_current_command`；
- 现有 `width`、`height` 继续表示 A 权威 cell 尺寸。

这些字段来自 tmux format 查询，不从输出正文推断。Web C 使用几何字段做“聚焦当前
Pane”的客户端裁切；它们不能被反向作为 resize 请求。Session ID、Window ID 和
Pane ID 继续作为稳定目标，所有名称只用于显示。

## 7. tmux 辅助操作

完整键盘输入始终可用；辅助层解决手机难以输入组合键和常用动作发现性问题。

首版辅助动作：

- 左右切分 Pane；
- 上下切分 Pane；
- 新建 Window；
- 上下左右选择 Pane；
- 切换 tmux zoom；
- 进入 copy mode；
- 关闭 Pane（必须二次确认）；
- 展示更多动作的可搜索面板。

A 读取当前 tmux Prefix 和 prefix key table，为每个语义动作返回检测到的快捷键说明。
UI 的悬浮提示展示真实绑定，不能假设 Prefix 永远是 `Ctrl+B`。如果用户未绑定某个
动作，提示显示“未绑定”。

点击一键动作时，C 发送稳定的语义 action_id；A 在该远程 client 当前上下文中执行
对应 tmux 命令并返回结果。B 只路由 action_id，不在控制平面实现 tmux 逻辑。

桌面端：

- “显示设置”和“Tmux 操作”位于最顶部标题栏同一高度；
- Tmux 操作可悬浮预览、点击固定并展开为响应式命令面板；
- 面板覆盖终端，不占用终端 rows/cols 或布局高度；
- 正常键盘直接交给 tmux；浏览器/操作系统保留的全局快捷键除外。

手机端：

- 提供粘滞 Ctrl、Alt、Shift、Esc、Tab 和 Tmux Prefix 键；
- Tmux Prefix 键支持“先按 Prefix、再按下一键”，无需物理同时按键；
- 快捷动作栏默认收起为悬浮把手；
- 点击后以覆盖式抽屉弹出，再次点击、下滑或完成动作后收起；
- 横屏和竖屏采用相同逻辑，不使用单独的常驻侧栏。

## 8. Computer 注册与认证

### 8.1 一次性注册码

电脑管理页的“添加电脑”调用现有注册能力，展示：

- 只显示一次的高熵注册码；
- 十分钟倒计时；
- 根据当前 B origin 生成的完整 `termflow login` 命令；
- 复制按钮和安全提示。

注册码十分钟过期且最多成功使用一次。消费必须改成单条条件更新或等价的数据库原子
事务，不能使用“先查询可用、再写 used_at”的竞态流程。

成功注册后，A 获得私有 Installation Credential。Web C 永远不显示、读取或保存
Installation/Instance Credential。

### 8.2 Web 管理员会话

浏览器不能依赖现有自定义 Authorization WebSocket header。Web 登录流程为：

1. 用户输入 B 的长期管理员 Token；
2. `POST /api/v1/admin/sessions` 校验 Token；
3. B 创建随机、高熵、限时的服务端会话；
4. 响应设置 HttpOnly、SameSite=Strict、Path=/ 的 Cookie；
5. HTTPS 生产环境必须设置 Secure，并使用 `__Host-` Cookie 前缀；
6. Web C 不在 localStorage、sessionStorage、IndexedDB 或 JS 状态中长期保存管理员
   Token；
7. 原有 Bearer Token 认证继续用于 curl 和非浏览器 C。

会话默认八小时过期，存储在单 B 进程内存中；B 重启会让 Web C 重新登录。退出登录
删除会话并立即关闭其活动终端 WebSocket。

Cookie 认证的状态变更请求和所有 WebSocket 握手必须校验精确 Origin allowlist。
会话与注册码响应设置 `Cache-Control: no-store`。生产部署要求 HTTPS/WSS；本地
loopback 开发可显式使用非 Secure 开发 Cookie。

## 9. B 的公开读模型与 API

新增面向所有未来 C 的中立 API：

```text
POST   /api/v1/admin/sessions
GET    /api/v1/admin/session
DELETE /api/v1/admin/session

GET    /api/v1/dashboard
GET    /api/v1/computers
GET    /api/v1/computers/{computer_id}
PATCH  /api/v1/computers/{computer_id}

PATCH  /api/v1/terms/{instance_id}
WS     /api/v1/terms/{instance_id}/terminal
```

`GET /api/v1/dashboard` 返回一个一致的只读快照：

- Computer 数量和在线状态；
- 在线 Term / 总 Term 数；
- 在线 Term 拓扑中的活动 Pane 数；
- 最近 24 小时成功或失败的输入/辅助操作审计数量；
- 按 Computer 分组的 Term summary。

Term summary 包含：

- instance_id；
- 最后已知 tmux Session name；
- Computer identity；
- online；
- Window/Pane 数量；
- 活动 Pane 的原始 `pane_current_command`；
- 最后在线时间。

终端正文不进入上述响应。现有 `/api/v1/instances`、topology、Pane 普通文本输入和
events WebSocket 保持兼容。

## 10. Web C 页面与组件

前端采用 Vue 3、TypeScript、Vite、Vue Router、Vitest 和 xterm.js，不引入大型
UI 框架。结构位于 `apps/clients/web/`。

### 10.1 登录页

- 输入管理员 Token；
- 登录成功后立即清空输入值；
- 只显示结构化、用户可理解的认证错误；
- 不把 Token 加入 URL、日志或浏览器持久存储。

### 10.2 总览控制台

- 顶部指标：在线 Term、活动 Pane、过去 24 小时交互；
- Computer 使用大卡片分组；
- 每个 Computer 大卡片内部以多行小卡片展示 Term；
- Term 行展示真实名称、在线状态、Window/Pane 数和原始当前命令；
- 点击 Term 进入完整 tmux 页面；
- 离线 Computer 明确说明本地 tmux 仍可能运行。

### 10.3 电脑管理

- 电脑显示名称、hostname、平台、版本、最后在线时间和 Term 数；
- 支持编辑显示名称；
- “添加电脑”打开一次性注册码面板；
- 本阶段不实现吊销、删除或远程安装。

### 10.4 Term 页面

- 顶部只保留返回、可编辑 Term/tmux 名称、Computer、连接状态、显示设置和 Tmux
  操作入口；
- 其余空间由 xterm.js 完整 tmux 画面占据；
- 显示设置为一个按钮，弹层以左侧空心/实心圆点展示四个竖排单选项；
- Tmux 操作面板是覆盖式浮层，不能挤压终端；
- 断线时保留最后画面并覆盖连接状态，但禁用输入；
- stream 重建时清空旧画面，等待新 tmux client 全屏重绘。

### 10.5 响应式规则

- 桌面保留最小标题栏，不常驻侧栏；
- 手机竖屏默认聚焦活动 Pane，并可切换全局视图；
- 手机横屏仍支持手动缩放、拖动和同一个可收起动作栏；
- 横竖屏分别记住观看模式和缩放比例；
- C 的任何视觉操作都不能产生 terminal resize 消息。

## 11. 三套主题与共享 Design Tokens

首版主题：

1. `graphite-signal`：石墨黑、冷灰、薄荷绿，默认；
2. `cloud-cobalt`：浅色背景、雾灰、钴蓝；
3. `midnight-indigo`：午夜紫、靛青、琥珀状态色。

`packages/design-tokens/` 定义语言中立的主题 token。组件只使用语义名称：

- canvas、surface、surface-raised；
- border、border-strong；
- text、text-muted；
- accent、accent-contrast；
- success、warning、danger；
- overlay、shadow；
- terminal-background、terminal-foreground 和基础 ANSI palette。

Vue 组件中禁止直接写主题十六进制颜色；测试扫描生产组件，确保颜色只出现在主题
定义和必要的外部资源中。用户选择保存在当前客户端本地偏好中，主题偏好不是凭据。

tmux 输出中的显式 ANSI 颜色和用户状态栏配色照常由终端字节控制；TermFlow 主题只
决定未指定颜色的终端默认 palette 与外围组件。

未来 Web 技术构建的 EXE/Linux 客户端可直接复用 token；原生 App 使用同一 JSON
语义表生成平台颜色资源。

## 12. 错误、离线与背压

- Computer/Term 离线：总览显示离线，终端入口不建立可输入通道；
- 打开过程中 A 离线：WebSocket 返回 `instance_offline` 并关闭；
- Pane/Window 在操作前消失：A 返回 `target_not_found`，UI 刷新终端状态；
- Bridge 队列满：拒绝新动作，不静默丢输入；
- C 消费过慢：只关闭该 C 终端订阅，不阻塞 Bridge 或其他 Term；
- B-C 网络断开：C 指数退避重连，输入在确认连接恢复前禁用且不离线排队；
- A-B 网络断开：A 上 tmux 继续运行，代理 PTY 在宽限后关闭；
- B 重启：Web 会话失效，重新登录后重新打开 tmux client；
- 关闭 Pane、Window 或其他破坏性辅助动作必须确认；
- 所有错误使用稳定 code，UI 文案不依赖服务端英文 message。

## 13. 隐私与审计

B 只持久化：

- Computer/Installation 元数据；
- Term/Instance 元数据和最后已知名称；
- 一次性注册码哈希和使用状态；
- 不含正文的审计：操作类型、Term、Pane、字节数、结果、错误码、时间。

B 不持久化：

- 终端输入字节；
- 终端输出字节；
- tmux 屏幕截图或录像；
- 管理员 Token、注册码明文、Installation/Instance Credential 明文；
- Agent 对话、项目文件或命令正文。

应用日志不得记录 WebSocket payload、Cookie、Authorization、注册码或完整终端 URL
参数。

## 14. 同镜像部署

Control Plane Dockerfile 使用多阶段构建：

1. Node 22 阶段执行 Web C 的锁定依赖安装、测试外的生产构建；
2. Python/uv 阶段安装 B；
3. 将 Web C dist 复制到 `/app/frontend-dist`；
4. FastAPI 挂载带 hash 的 `/assets`；
5. 非 `/api/*`、非 WebSocket 的客户端路由回退到 `index.html`；
6. 未知 `/api/*` 必须保持 JSON 404，不能返回 SPA；
7. `/healthz` 和现有 Bridge 端点保持不变。

生产仍是一个 B 容器、一个 Uvicorn worker和一个 SQLite volume。前端没有独立 Nginx、
端口、CORS 或部署步骤。

## 15. 测试与验收

### 15.1 协议与 A

- terminal 消息模型、Base64、大小限制和未知字段；
- 使用真实 tmux 3.2+ 的 PTY 集成测试；
- 完整状态栏/分屏输出可由 xterm 消费；
- Ctrl+C、Ctrl+B、方向键、Tab、Esc、IME 和粘贴字节到达 tmux client；
- rename-session 双向同步；
- current_command 与 Pane 几何字段来自 tmux；
- A 尺寸选择排除远程代理 client；
- C 侧不存在 resize 后，A 的尺寸不改变；
- disconnect/reconnect、30 秒宽限、gap 后全屏重绘；
- 每 Term 只有一个可输入远程 client。

### 15.2 B

- 管理员 Token 换取 HttpOnly 会话，Token 不进入响应正文和日志；
- Cookie 属性、Origin allowlist、过期、退出和活动 WS 关闭；
- Bearer Token 兼容；
- 注册码并发消费测试证明最多成功一次；
- Computer 元数据、改名、dashboard 聚合和 24 小时审计；
- terminal channel 路由、替换、背压、超时和离线错误；
- B 的数据库和日志中不存在终端正文。

### 15.3 Web C

- 登录、总览、Computer 分组、电脑管理和注册链接；
- xterm 二进制输入输出、stream 重建和错误 overlay；
- 完整键盘、粘滞修饰键、Prefix 状态和辅助动作确认；
- 桌面显示设置单选菜单；
- 手机横竖屏、缩放、拖动、聚焦 Pane 与可收起动作栏；
- 三主题切换与本地偏好；
- 生产组件无硬编码主题颜色；
- 生产组件无 Agent 品牌识别或硬编码状态。

### 15.4 集成与交付

- Python 全量测试、类型检查和 lint；
- 前端 Vitest、类型检查和生产构建；
- Docker 构建；
- 同容器 root、嵌套 SPA route、静态 asset、API 404 和 health smoke；
- 浏览器真实登录、生成注册码、总览、打开 Term、完整键盘、分屏、改名、主题、手机
  viewport 和断线重连；
- `git diff --check` 与文档契约测试。

## 16. 并行实施边界

共享协议和 API 契约以本文档及后续实施计划为准，实施按无文件重叠的三条线组织：

1. A+B agent：`packages/protocol`、`apps/node`、`apps/control-plane` 及对应测试；
2. Web C agent：`apps/clients/web`、`packages/design-tokens` 及前端测试；
3. 主 agent：Docker 静态集成、跨端 E2E、文档、依赖锁协调、代码审查和最终验证。

Web C agent 不修改 Python 协议；A+B agent 不修改 Web 组件。需要调整共享契约时先通知
主 agent，由主 agent 同步两边，避免并行实现发生漂移。

## 17. 明确不在本阶段实现

- B 端 AI Agent、STT、TTS 和自然语言操作；
- 手机原生 App、Electron/原生 EXE 或 Linux GUI 安装包；
- 多用户、角色权限、OIDC/SSO；
- 多个并发可输入远程控制者和只读观察者；
- Computer Credential 吊销、远程卸载或远程安装；
- 从 C 创建新的 Term；
- B 多实例、Kafka/NATS/Redis；
- 终端正文持久化、搜索、录像或回放；
- C 改变 A 的终端尺寸。

## 18. 参考实践

- Coder Web Terminal：xterm.js、WebSocket、代理与重连
  <https://coder.com/docs/user-guides/workspace-access/web-terminal>
- Coder Agent Architecture：A 主动连接控制平面，无入站端口
  <https://coder.com/docs/ai-coder/agents/architecture>
- Apache Guacamole Architecture：服务器托管但协议解耦的 Web client
  <https://guacamole.apache.org/doc/1.5.2/gug/guacamole-architecture.html>
- Tailscale Auth Keys：一次性设备注册凭据
  <https://tailscale.com/docs/features/access-control/auth-keys>
- Teleport Join Tokens：短时机器加入凭据
  <https://goteleport.com/docs/installation/agents/join-token/>
- OWASP Session Management：HttpOnly/Secure/SameSite Cookie 与浏览器存储边界
  <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
- OWASP WebSocket Security：Origin、会话过期、退出与日志要求
  <https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html>
