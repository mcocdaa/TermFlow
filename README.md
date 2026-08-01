# TermFlow

TermFlow V1 让你在电脑 A 上运行彼此隔离的 tmux 终端，并通过服务 B 的 HTTP/WebSocket
接口远程查看已有 Pane、发送普通文字。一个 `termflow new` 对应一个独立 Instance（一个
A），不是一台物理电脑对应一个 A。B 暂时断线时，A 的 tmux 和本地操作继续运行。

V1 只实现 A+B。手机 App、网页控制器等 C 端尚未实现；Agent、STT/TTS、特殊键、远程
创建/删除 Pane 和 Kafka 也不在 V1 范围内。

## 环境要求

- Linux、macOS 或 WSL；
- Python 3.12；
- [uv](https://docs.astral.sh/uv/)；
- tmux 3.2 或更高版本。

## 快速开始

安装工作区依赖：

```bash
uv sync --frozen --all-packages
```

本地启动 B（仅用于 loopback 开发）：

```bash
export TERMFLOW_ADMIN_TOKEN='replace-with-a-random-admin-token'
export TERMFLOW_DATABASE_URL='sqlite+aiosqlite:///./data/termflow.db'
export TERMFLOW_ALLOW_INSECURE_LOOPBACK=true
uv run --package termflow-control-plane termflow-control serve --host 127.0.0.1 --port 8000
```

另一个终端创建十分钟有效的一次性注册码：

```bash
uv run --package termflow-control-plane termflow-control enrollment create
```

在电脑 A 登录，然后创建并附着一个 Instance：

```bash
uv run --package termflow-node termflow login \
  --server http://127.0.0.1:8000 \
  --enrollment-token '<一次性注册码>'
uv run --package termflow-node termflow new --name project-a
```

使用 tmux 默认 detach 键 `Ctrl+B` 后按 `D` 返回普通 shell。之后可执行：

```bash
uv run --package termflow-node termflow list
uv run --package termflow-node termflow attach '<instance-uuid>'
uv run --package termflow-node termflow doctor
uv run --package termflow-node termflow kill '<instance-uuid>'
```

B 的容器化启动参见 [deploy/compose.yaml](deploy/compose.yaml) 与
[deploy/env.example](deploy/env.example)。默认只映射 `127.0.0.1:8000`；公网部署应在 B
前放置 HTTPS/WSS 反向代理。

## 文档

- [架构与进程边界](docs/architecture.md)
- [V1 协议](docs/protocol.md)
- [安全与隐私](docs/security.md)
- [API 调用示例](docs/api-examples.md)
- [排障指南](docs/troubleshooting.md)

设计与实施计划保存在 `docs/superpowers/`，它们是工程记录；以上文档描述实际 V1
使用方式。
