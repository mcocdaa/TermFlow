# TermFlow V1 协议

## 通用 Envelope

Bridge 与 B 的 JSON 消息均使用版本 1：

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

未知主版本会被拒绝。Bridge 和资源 API 的长期/短期凭据通过
`Authorization: Bearer ...`（原生资源再配合 DPoP）传递，不放在 URL。只有浏览器登录、
TOTP 设置和 CLI token 交换这些明确的 re-auth 请求才在 HTTPS JSON body 中短暂提交 Admin
Token；原生 OAuth 授权页的批准请求也属于这个明确的浏览器 re-auth 请求。服务端不会把它写入
Cookie、URL 或日志。

## HTTP API

| 方法 | 路径 | 凭据 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 无 | 健康检查 |
| `POST` | `/api/v1/enrollment-tokens` | Admin | 创建一次性注册码 |
| `POST` | `/api/v1/admin/sessions` | Admin | 创建 Web C 会话 |
| `POST` | `/api/v1/admin/sessions/{challenge_id}/totp` | 挑战 + 一次性验证码 | 完成启用 TOTP 后的 Web C 登录 |
| `GET/DELETE` | `/api/v1/admin/session` | Cookie | 检查或删除当前 Web C 会话 |
| `GET` | `/api/v1/admin/totp` | Web Cookie | 查询 TOTP 状态 |
| `POST` | `/api/v1/admin/totp/setups` | Web Cookie + Admin/当前验证码 | 创建验证器绑定挑战 |
| `POST` | `/api/v1/admin/totp/setups/{setup_id}/confirm` | Web Cookie + 一次性验证码 | 确认验证器绑定 |
| `POST` | `/api/v1/admin/totp/enable` | Web Cookie + Admin/一次性验证码 | 启用登录保护 |
| `DELETE` | `/api/v1/admin/totp` | Web Cookie + Admin/一次性验证码 | 关闭登录保护 |
| `POST` | `/api/v1/admin/cli-tokens` | Admin + 可选 TOTP | 为 CLI 交换短期 Bearer token |
| `GET` | `/.well-known/oauth-authorization-server` | 无 | 原生客户端发现 OAuth 端点 |
| `GET/POST` | `/api/v1/oauth/authorize` | 原生请求；浏览器确认时 Admin + 可选 TOTP | 创建、预览和批准原生客户端授权 |
| `POST` | `/api/v1/oauth/token` | PKCE + DPoP | 交换原生 access/refresh token |
| `POST` | `/api/v1/oauth/revoke` | 原生授权 | 撤销原生 token |
| `GET/PATCH/DELETE` | `/api/v1/admin/clients[/{client_id}]` | Cookie | 查看、重命名或撤销已授权原生客户端 |
| `POST` | `/api/v1/installations/enroll` | 注册码 | 换取 Installation Credential |
| `POST` | `/api/v1/instances/register` | Installation | 注册/轮换该 Installation 所属 Instance |
| `GET` | `/api/v1/instances` | Admin | 列出 Instance 与在线状态 |
| `GET` | `/api/v1/instances/{instance_id}` | Admin | 查看一个 Instance |
| `GET` | `/api/v1/instances/{instance_id}/topology` | Admin | 获取在线实时拓扑 |
| `POST` | `/api/v1/instances/{instance_id}/panes/{pane_id}/input` | Admin | 向一个已有 Pane 发普通文本 |
| `GET` | `/api/v1/dashboard` | Admin/Cookie | 获取控制台指标与 Computer/Term 列表 |
| `GET/PATCH` | `/api/v1/computers[/{installation_id}]` | Admin/Cookie | 列出或重命名 Computer |
| `PATCH` | `/api/v1/terms/{instance_id}` | Admin/Cookie | 重命名真实 tmux Session |

输入正文只有 `text` 与 `submit`。调用方必须发送 UUID `Idempotency-Key`。`text` 最大
16 KiB UTF-8，拒绝 C0/C1、DEL、ESC 和 Ctrl+C 等控制字符；`submit=true` 只追加 Enter。
B 收到 Bridge 的匹配确认后才返回成功。A 离线时立即返回 409，不排队、不延迟执行。

### 浏览器与原生登录

Web C 先向 `/api/v1/admin/sessions` 提交 Admin Token；启用 TOTP 时响应
`202` 和 `challenge_id`，再向 `/api/v1/admin/sessions/{challenge_id}/totp` 提交 6 位验证码，
成功后 B 才设置 HttpOnly Cookie。所有浏览器写操作都必须带与
`TERMFLOW_PUBLIC_BASE_URL` 一致的 Origin。

Tauri App/EXE 不在自己的登录界面收集管理员 Token。它通过 OAuth/PKCE 发起授权，系统浏览器
显示 Web C 的授权页；用户在该页确认客户端和 scopes，启用 TOTP 时再输入一次性验证码。回调
只包含一次性授权结果，原生客户端随后使用 DPoP 交换短期 access/refresh token。

启用 TOTP 后，原始 Admin Bearer 不再直接通过受保护资源检查；运维脚本应调用
`/api/v1/admin/cli-tokens`（同时提交当前验证码）获得带 scopes 的 CLI token，再把该 token
放入 `Authorization` Header。这样不会把“一次性验证码”扩散到每个资源请求。

## Bridge WSS

`/api/v1/bridge/connect` 使用 Instance Credential。A 到 B 的消息：

- `bridge.hello`、`bridge.heartbeat`；
- `topology.snapshot`、`topology.changed`；
- `pane.output`、`stream.gap`；
- `command.result`。

B 到 A 保留 Pane 自动化消息，并增加完整 terminal 通道：

- `pane.input`：一个已有 Pane 的普通文字和可选 Enter；
- `pane.replay_request`：`pane_id + stream_id + after_seq` 回放游标；
- `terminal.open/input/action/close`：打开真实 tmux client、发送原始输入或语义动作。

A 返回 `terminal.opened/output/size/bindings/action_result/closed`。A-B 原始字节使用严格
Base64，并以 `terminal_id`、`stream_id` 和递增 `seq` 区分连接与回放。语义动作只覆盖
已批准的 split、新 Window、Pane 导航、zoom、copy mode 和确认后的 close Pane。

## Web C 完整终端 WebSocket

`/api/v1/terms/{instance_id}/terminal` 使用浏览器 HttpOnly Cookie 或原生 Bearer。B→C
与 C→B 的终端字节都使用二进制帧；文本帧只承载 `terminal.ready`、A 权威 size、binding
snapshot、action、error 和 closed 等 JSON 控制事件。C 没有 resize 控制事件。

单个二进制帧不超过 65,536 bytes。每个 Term 只有一个输入 owner；新连接替换旧连接。
A 在 Bridge 断线后保留 30 秒、最多 1 MiB 内存缓冲；无法证明连续回放时创建新 stream，
由新的 tmux client 完成全屏重绘。B 不保存 terminal replay。

## 事件 WSS 与输出

Admin 通过 `/api/v1/events?instance_id=<uuid>` 订阅。可选回放参数 `pane_id`、`stream_id`
和 `after_seq` 必须一起提供。该 WebSocket 只读，不接受客户端命令。

tmux 输出可能包含任意字节与 ANSI 序列，因此 `pane.output.data_base64` 是严格 Base64。
每个 Pane 的内存环包含 `stream_id` 和从 1 递增的 `seq`。覆盖、Bridge 重启、网络背压或
tmux control mode 暂停时会发送 `stream.gap`；随后 Bridge 抓取当前屏幕并开始新 stream。
B 不保存回放历史。

## 典型错误

统一格式为 `{"error":{"code":"instance_offline","message":"...","request_id":"uuid"}}`。
常见 code 包括 `unauthorized`、`instance_not_found`、`instance_offline`、`pane_not_found`、
`backpressure`、`command_timeout` 与 `outcome_unknown`。错误与日志不会回显输入正文。
