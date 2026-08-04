# TermFlow

TermFlow 让你在 Computer A 上运行彼此隔离的 tmux Term，再通过服务 B 和 Web C 从电脑
或手机远程使用完整 tmux 控制台。B 暂时断线、浏览器关闭或手机离线时，A 的 tmux、Pane
进程和本地操作都继续运行。

产品层级固定为 `Computer → Term → Window → Pane`：一次 `termflow login` 注册一台
Computer；一个 `termflow new` 创建一个独立 Term（私有 tmux server/session）。当前不
实现 Agent、STT/TTS 或远程创建 Term；Web C 只操控 A 上已经存在的 Term。

## 正式安装与部署

正式版本由 GitHub Release 发布；Actions 手动打包页的 artifact 仅用于测试，不是长期下载地址。

Computer A 当前提供 Linux x86_64 一条命令安装包。目标机器需要 `tmux 3.2+`，安装器会校验
Linux、架构、`curl`、`sha256sum` 与 tmux 版本，再把指定版本安装在当前用户的 `~/.local`，不会
使用 `sudo`、修改 shell 配置或创建 systemd 服务：

```bash
curl -fsSL https://github.com/mcocdaa/TermFlow/releases/download/vX.Y.Z/install-termflow-node.sh | bash
termflow login --server https://termflow.example.com --code '<一次性注册码>'
termflow new --name project-a
```

将 `vX.Y.Z` 替换为需要的精确 GitHub Release tag。安装器会保留旧版本；回退 A 时，只需重新运行
旧 tag 的同一命令。上面的 URL 使用官方仓库；Fork 发布时把 `mcocdaa/TermFlow` 替换为
自己的 `<owner>/<repository>`。确保 `~/.local/bin` 已在当前 shell 的 `PATH` 中。

B 与 Web C 由当前 checkout 的同一份 Dockerfile 构建。先切换到需要部署的精确源码 tag 或 commit，
再复制并填写管理员 token 和公开 URL：

```bash
cp .env.example .env
# 编辑 TERMFLOW_ADMIN_TOKEN 和实际的 TERMFLOW_PUBLIC_BASE_URL。
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Windows、Linux、macOS、Android 与 iOS Simulator 客户端均作为同一 GitHub Release 的 assets 发布。
当前 Windows 包未签名；iOS asset 仅能用于 Simulator，不能安装到实体 iPhone。

Actions 页面还提供三套独立的手动测试包：A 的
`termflow-node-linux-x86_64.tar.gz` 和安装器、B + Web C 的
`termflow-control-plane.tar`，以及原生 C 的 Windows NSIS、Linux deb/AppImage、macOS、
Android 和 iOS Simulator 包。手动 Artifact 使用稳定名称并保留 14 天；只有 Tag Release 才会推送 GHCR
和创建永久 GitHub Release。下载 B 的手动 tar 后可执行
`docker load -i termflow-control-plane.tar`，但它不会自动修改现有 Compose 部署。

从 Actions 下载的 artifact 可按下面方式做离线安装验收。A 的安装器通过
`Path.cwd().as_uri()` 把本地目录转换为 `file://` URL，因此下载目录包含空格或括号时也能工作：

```bash
cd "/path/to/termflow-node-linux-x86_64"
TERMFLOW_RELEASE_BASE_URL="$(python3 -c 'from pathlib import Path; print(Path.cwd().as_uri())')" \
  ./install-termflow-node.sh
~/.local/bin/termflow --version
~/.local/bin/termflow doctor
```

B + Web C 的 tar 先导入 Docker，并保留 `docker load` 输出的完整镜像名；从源码 Compose
切换到该 artifact 时只执行 `down`，不要加 `--volumes`，这样会保留 `termflow-data`：

```bash
cd "/path/to/termflow-control-plane"
IMAGE_NAME="$(docker load -i termflow-control-plane.tar | sed -n 's/^Loaded image: //p')"
test -n "$IMAGE_NAME"

cd /path/to/TermFlow
set -a; source .env; set +a
docker compose --env-file .env -f deploy/compose.yaml down
docker run -d --name termflow-control-plane --restart unless-stopped \
  --env-file .env \
  --publish "127.0.0.1:${TERMFLOW_HOST_PORT:-8765}:8000" \
  --volume "${TERMFLOW_DATA_VOLUME:-termflow-data}:/app/data" \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_STATIC_DIR=/app/frontend-dist \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK="${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/data/totp-master-key \
  "$IMAGE_NAME"
curl -fsS http://127.0.0.1:8765/healthz
```

上面第二段中的 `/path/to/TermFlow` 替换为本仓库路径；如果没有旧的 Compose 服务，
`docker compose ... down` 也可以直接执行。这个流程不会删除或重建数据卷。

如何从 Actions 页面或 `gh workflow run` 手动构建、如何按平台选择 C、Tag 触发顺序、产物保留期
和签名限制，见 [GitHub Actions 构建与发布](docs/github-actions.md)。

正式构建版本的解析顺序固定为
`Git Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0`。Tag Release 直接使用 `vX.Y.Z`
（以及受支持的 prerelease/build metadata）中的版本；手动 workflow 可填写可选版本，本地构建可设置
`TERMFLOW_BUILD_VERSION=1.2.3`。两者都没有时使用明确的开发版本
`0.0.1-dev.0`，不会被误认为正式 Release。

## 源码开发环境要求

- Linux、macOS 或 WSL；
- Python 3.12；
- [uv](https://docs.astral.sh/uv/)；
- tmux 3.2 或更高版本。

## 源码开发快速开始

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

另一个终端创建 60 秒有效的一次性注册码：

```bash
uv run --package termflow-control-plane termflow-control enrollment create
```

在电脑 A 登录，然后创建并附着一个 Term：

```bash
uv run --package termflow-node termflow login \
  --server http://127.0.0.1:8000 \
  --code '<一次性注册码>'
uv run --package termflow-node termflow new --name project-a
```

使用 tmux 默认 detach 键 `Ctrl+B` 后按 `D` 返回普通 shell。之后可执行：

```bash
uv run --package termflow-node termflow list
uv run --package termflow-node termflow attach '<instance-uuid>'
uv run --package termflow-node termflow doctor
uv run --package termflow-node termflow kill '<instance-uuid>'
```

B+Web C 的容器化启动参见 [deploy/compose.yaml](deploy/compose.yaml) 与
[.env.example](.env.example)。默认只映射 `127.0.0.1:8765`；公网部署应在 B
前放置 HTTPS/WSS 反向代理。

## 日志位置

Computer A 的 CLI 与 Bridge 写入结构化 JSONL 日志，默认 10 MiB 轮转并保留 5 份：Linux 为
`~/.local/state/termflow/log/termflow.log`，macOS 为 `~/Library/Logs/termflow/termflow.log`，
Windows 为 `%LOCALAPPDATA%\\termflow\\Logs\\termflow.log`。Tauri 客户端写入系统应用日志目录中的
`termflow-client.log`。日志不包含 token、验证码、PKCE、Cookie 或终端输入输出；Web C 不写本地
日志，使用响应头 `X-Request-ID` 关联 B 日志。B 在 Docker 中只输出 stdout/stderr：

```bash
docker compose --env-file .env -f deploy/compose.yaml logs -f control-plane
```

## 文档

- [架构与进程边界](docs/architecture.md)
- [V1 协议](docs/protocol.md)
- [安全与隐私](docs/security.md)
- [API 调用示例](docs/api-examples.md)
- [Web C 使用与主题](docs/web-client.md)
- [部署与恢复](docs/operations.md)
- [GitHub Actions 构建与发布](docs/github-actions.md)
- [排障指南](docs/troubleshooting.md)

设计和实施历史见 [`docs/superpowers/README.md`](docs/superpowers/README.md)；其中的历史计划
是工程记录，不替代上述当前版本使用说明。
