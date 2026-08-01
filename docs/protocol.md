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

未知主版本会被拒绝。凭据只通过 `Authorization: Bearer ...` Header 传递，不放在 URL 或
消息正文。

## HTTP API

| 方法 | 路径 | 凭据 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 无 | 健康检查 |
| `POST` | `/api/v1/enrollment-tokens` | Admin | 创建一次性注册码 |
| `POST` | `/api/v1/installations/enroll` | 注册码 | 换取 Installation Credential |
| `POST` | `/api/v1/instances/register` | Installation | 注册/轮换该 Installation 所属 Instance |
| `GET` | `/api/v1/instances` | Admin | 列出 Instance 与在线状态 |
| `GET` | `/api/v1/instances/{instance_id}` | Admin | 查看一个 Instance |
| `GET` | `/api/v1/instances/{instance_id}/topology` | Admin | 获取在线实时拓扑 |
| `POST` | `/api/v1/instances/{instance_id}/panes/{pane_id}/input` | Admin | 向一个已有 Pane 发普通文本 |

输入正文只有 `text` 与 `submit`。调用方必须发送 UUID `Idempotency-Key`。`text` 最大
16 KiB UTF-8，拒绝 C0/C1、DEL、ESC 和 Ctrl+C 等控制字符；`submit=true` 只追加 Enter。
B 收到 Bridge 的匹配确认后才返回成功。A 离线时立即返回 409，不排队、不延迟执行。

## Bridge WSS

`/api/v1/bridge/connect` 使用 Instance Credential。A 到 B 的消息：

- `bridge.hello`、`bridge.heartbeat`；
- `topology.snapshot`、`topology.changed`；
- `pane.output`、`stream.gap`；
- `command.result`。

B 到 A 的消息只有：

- `pane.input`：一个已有 Pane 的普通文字和可选 Enter；
- `pane.replay_request`：`pane_id + stream_id + after_seq` 回放游标。

V1 没有通用 tmux 命令、特殊键通道，也没有远程 Pane/Window 生命周期操作。

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
