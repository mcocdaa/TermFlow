# TermFlow 架构

## 心智模型

一次 `termflow login` 注册一个 Computer。一个 `termflow new` 创建一个 Term；每个 Term
有自己的 UUID、私有状态目录、tmux socket、tmux server、受管理 Session 和 Bridge
进程。同一 Computer 可以同时运行多个 Term；它们互不共享 socket、Bridge 凭据或重连
状态。Term 下面是 tmux 原生的 Window 和 Pane。

```text
Computer A                              B Docker
┌──────────────────────────────┐       ┌────────────────────────────┐
│ 本地 tmux client             │       │ B：FastAPI 单 worker       │
│ 私有 tmux server/session     │ WSS   │ 身份/元数据 SQLite         │
│ control-mode + 远程 client PTY├─────▶│ 在线路由/终端转发仅内存    │
│ 每 Term 一个主动 Bridge      │       │ Web C：独立 Vue SPA        │
└──────────────────────────────┘       └─────────────┬──────────────┘
                                                    │ HTTP/WS
                                           浏览器/手机/未来 App、EXE
```

## 连接所有权

Bridge 主动建立并长期维护到 B 的一条 WSS，这让 NAT 后面的电脑无需开放端口。CLI 只负责
登录 Computer，以及创建、附着、诊断和停止本地 Term；CLI 退出或 tmux client detach
都不会终止 Bridge 或 tmux server。B 重启或网络中断后，每个 Bridge 独立退避重连，并
重新上报完整拓扑。B/C 断开时 Term 和其中的 Pane 继续运行。

Web C 看到的不是 B 拼装的 Pane 文本，而是 A 在 PTY 中附着的真实 tmux client 输出，
因此状态栏、边框、Window 切换、copy mode 和前缀键都由 tmux 自己绘制。Web C 是协议
独立的 C；它与 B 放进同一 Docker 镜像只是部署选择。这个镜像的 runtime 只提供 B 的
HTTP/WS 进程和 Web 静态文件，A、Tauri 工程以及构建工具不在镜像里。

## 尺寸与显示

终端 rows/cols 以 A 权威尺寸为准：优先最近活动的本地 tmux client，没有本地 client 时
使用 A 最后观察值，再兜底 80×24。C 不能改变 A 的 rows/cols。网页上的 50%、75%、
100%、适应窗口、手机双指缩放、拖动和聚焦 Pane 都只改变 C 的 viewport；显式“tmux
zoom”动作才会改变真实 Session 布局。

## B 的边界

B 是单 worker：在线连接、Pane 拓扑、远程 terminal owner 和订阅队列都在进程内存中，因此不得横向启动
多个 worker。SQLite 只保存 Installation、Instance、token 哈希、一次性注册码状态与
不含正文的审计元数据。终端输入、输出、屏幕快照和短期重连缓冲不持久化。

多 A 并不要求 Kafka。只有未来 B 需要多副本、跨进程路由时，才应在抽象边界后评估
NATS、Kafka 或其他外部消息系统。

## 客户端与未来 Agent

Web C 只通过公开 HTTP/WebSocket 契约与 B 通信，不读取 B 数据库。未来 App、EXE、Linux
桌面包与它平级复用同一契约，并通过系统浏览器的 OAuth/PKCE 授权而不是复制 Web 登录逻辑。
STT 可在 C 或独立服务完成；未来规划型 Agent 可放在 B
附近，但必须经过同一鉴权、审计和输入接口，不能绕过协议直接碰 A 的 tmux socket。
