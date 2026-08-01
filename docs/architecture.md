# TermFlow V1 架构

## 心智模型

一个 `termflow new` 创建一个 A，也就是一个 Instance。每个 Instance 有自己的 UUID、
私有状态目录、tmux socket、tmux server、受管理 Session 和 Bridge 进程。同一电脑可以
同时运行 A1、A2、A3；它们互不共享 socket、Bridge 凭据或重连状态。

```text
电脑 A                                      服务 B
┌──────────────────────────────┐           ┌─────────────────────────┐
│ 本地 tmux client             │           │ FastAPI 单 worker       │
│          │                   │           │                         │
│ 私有 tmux server/socket      │           │ 身份/元数据 SQLite      │
│          │ control mode      │           │ 在线连接/拓扑仅内存     │
│ 每 Instance Bridge ──────────┼── WSS ───▶│ HTTP + WebSocket API    │
└──────────────────────────────┘           └────────────┬────────────┘
                                                       │
                                              未来 C：手机/网页/EXE
```

## 连接所有权

Bridge 主动建立并长期维护到 B 的一条 WSS，这让 NAT 后面的电脑无需开放端口。CLI 只负责
创建、附着、诊断和停止本地 Instance；CLI 退出或 tmux client detach 都不会终止 Bridge
或 tmux server。B 重启或网络中断后，每个 Bridge 独立退避重连，并重新上报完整拓扑。

## B 的边界

B V1 是单 worker：在线连接、Pane 拓扑和订阅队列都在进程内存中，因此不得横向启动
多个 worker。SQLite 只保存 Installation、Instance、token 哈希、一次性注册码状态与
不含正文的审计元数据。终端输入、输出和屏幕快照不持久化。

多 A 并不要求 Kafka。只有未来 B 需要多副本、跨进程路由时，才应在抽象边界后评估
NATS、Kafka 或其他外部消息系统。

## C 与 Agent 的未来位置

V1 没有 C。未来 C 只和 B 通信，选择在线 Instance/已有 Pane、显示 Base64 终端字节并
调用同一普通文本输入 API。STT 可在 C 或独立服务完成；规划型 Agent 更适合放在 B
附近，但必须通过同一鉴权、审计和 Pane 输入 API，不能绕过协议直接碰 A 的 tmux socket。
