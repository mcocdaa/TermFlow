# TermFlow

TermFlow 让你通过浏览器、手机或原生客户端远程使用另一台电脑上的 tmux 终端。

```text
手机浏览器 / 桌面浏览器 / Windows、Linux、macOS、Android、iOS Simulator 客户端
                                  ↓ HTTPS / WSS
                         Control Plane B + Web C
                                  ↑ A 主动连接
                    Computer A（tmux、命令和工作目录）
```

关闭客户端或短暂断网不会停止 A 上运行的命令。v0.2.0 在
`Computer → Term → Window → Pane` 之上加入 Agent Broker（OpenCode 后端、人工审批、
可回放的 Agent 事件流和固定 Compose 参考部署）；语音能力不属于这个版本。

## 安装 B + Web C

### 发布镜像 Compose（推荐，含 Agent）

发布镜像同时包含 B、Web C 和 Agent Broker。下面的流程从 GitHub 取部署文件，Compose
只拉取 GHCR 上已发布的镜像，不在本地构建，也不需要 checkout 源码：

```bash
TAG=v0.2.0  # 换成 Release 页面的精确 tag
BASE="https://raw.githubusercontent.com/mcocdaa/TermFlow/${TAG}"
mkdir -p termflow/provider-egress && cd termflow
curl -fsSLO "${BASE}/deploy/compose.release.yaml"
curl -fsSLO "${BASE}/deploy/opencode-config.yaml"
curl -fsSLo provider-egress/squid.conf "${BASE}/deploy/provider-egress/squid.conf"
curl -fsSLo .env "${BASE}/.env.example"
chmod 0600 .env
```

按 `.env` 注释生成 `TERMFLOW_ADMIN_TOKEN`、`OPENCODE_SERVER_*`、
`OPENCODE_AGENT_MCP_TOKEN`、`DEEPSEEK_API_KEY`，把 `TERMFLOW_RELEASE_IMAGE_TAG` 设为上面的
`TAG`，并按可核验证据填写 DeepSeek disclosure 字段（没有证据时 B 会 fail closed）。
然后启动并检查：

```bash
docker compose --env-file .env -f compose.release.yaml up -d
curl -fsS "http://127.0.0.1:${TERMFLOW_HOST_PORT:-8765}/healthz"
```

首次启动需要十几秒，`healthz` 立即失败时稍等重试。B 的 Web/API 默认发布到
`127.0.0.1:${TERMFLOW_HOST_PORT:-8765}`，数据保存在 `termflow-data`、
`termflow-totp-key`、`termflow-opencode-data` 三个命名卷。升级或回退只改
`TERMFLOW_RELEASE_IMAGE_TAG`，再 `docker compose --env-file .env -f compose.release.yaml pull`
和 `up -d`；不要删除数据卷，也不要用 `latest` 做需要复现的部署（它只随稳定 tag 移动）。
公网部署时，把 HTTPS/WSS 反向代理指向该端口，并把 `TERMFLOW_PUBLIC_BASE_URL` 设置为
用户实际访问的 HTTPS origin，`TERMFLOW_ALLOW_INSECURE_LOOPBACK` 保持 false。

### 单容器 B（仅终端，不含 Agent）

只需要终端、不需要 Agent 时，可以用已发布镜像直接运行 B；下面的命令使用本地目录保存
数据库与 TOTP 主密钥，并通过宿主机 `127.0.0.1:8000` 发布 Web/API：

```bash
mkdir -p data totp-secrets
docker network create termflow-net

docker run -d \
  --name termflow-control-plane \
  --restart unless-stopped \
  --network termflow-net \
  --publish 127.0.0.1:8000:8000 \
  --volume "$PWD/data:/app/data" \
  --volume "$PWD/totp-secrets:/app/totp-secrets" \
  --env TERMFLOW_ADMIN_TOKEN='<至少 32 字节的管理员 token>' \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_PUBLIC_BASE_URL=https://termflow.example.com \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK=false \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/totp-secrets/totp-master-key \
  ghcr.io/mcocdaa/termflow-control-plane:v0.2.0
```

检查服务（首启需要十几秒）：

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

单容器 B 不含 Agent 运行时，Agent 页面会显示 `provider_selection_unavailable`。
公网部署与反向代理要求和上面相同。B 启动后，在 Web C 的 Computers 页面生成
Computer A 使用的一次性注册码。

B 的 Web/API 通过宿主机 loopback 端口发布；A 主动连接 B，是连接 B 的客户端，不是 B 的网关。

发布镜像 Compose 是生产部署路径。开发或验证源码时，使用
[部署与恢复](docs/operations.md) 中的 `docker compose -f deploy/compose.yaml` 本地参考
部署（project `termflow-v020-local`）；它与发布 Compose 的服务形状一致，但会从当前
checkout 构建镜像。两种 B 部署对应的 A 网络和服务地址不同，Docker A 一节分别说明。

## 安装 Computer A

### Linux

Linux x86_64 需要 tmux 3.2 或更高版本：

```bash
curl -fsSL \
  https://github.com/mcocdaa/TermFlow/releases/download/v0.1.0/install-termflow-node.sh \
  | bash

termflow login \
  --server https://termflow.example.com \
  --code '<Web C 生成的一次性注册码>'

termflow new --name project-a
```

使用 tmux 默认的 `Ctrl+B`、再按 `D` 可退出当前界面，Term 会继续运行。重新进入：

```bash
termflow attach project-a
```

### Docker

Docker A 是一个后台常驻的计算节点。身份和工作目录保存在本地目录，A 不开放端口，只主动
通过 WebSocket 连接 B。A 必须与 B 加入同一个普通 bridge 网络，并使用与 B 相同版本的镜像：

- 已发布版本：把下文的镜像换成 Release 页面对应的 tag，例如
  `ghcr.io/mcocdaa/termflow-node:vX.Y.Z`，不要使用 `latest`；
- 从源码验证：在仓库根目录运行 `scripts/build-node-image.sh termflow-node:v0.2.0`。

先在 Web C 的 Computers 页面生成一次性注册码；无界面环境可以用管理员 token 调用 API
（默认 60 秒内有效，请在启动 A 前立即生成；端口与 B 的发布端口一致，默认 8765）：

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN" \
  http://127.0.0.1:8765/api/v1/enrollment-tokens
```

下面假设 B 是上文的发布 Compose project `termflow`：A 加入它的默认网络
`termflow_default`，并通过服务名 `control-plane` 访问 B：

```bash
mkdir -p termflow-node-identity termflow-node-work

docker run -d \
  --name termflow-node \
  --restart unless-stopped \
  --network termflow_default \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add SETUID \
  --cap-add SETGID \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp \
  --volume "$PWD/termflow-node-identity:/home/termflow" \
  --volume "$PWD/termflow-node-work:/work" \
  --env TERMFLOW_SERVER=http://control-plane:8000 \
  --env TERMFLOW_CODE='<一次性注册码>' \
  --env TERMFLOW_ALLOW_INSECURE_HTTP=true \
  --env TERMFLOW_NEW=demo \
  termflow-node:v0.2.0
```

如果 B 用本页前面的单容器示例运行，则改为 `--network termflow-net` 和
`TERMFLOW_SERVER=http://termflow-control-plane:8000`；如果是源码 checkout 的本地参考
部署（project `termflow-v020-local`），网络改成 `termflow-v020-local_default`，服务地址
同样是 `http://control-plane:8000`。
`TERMFLOW_ALLOW_INSECURE_HTTP=true` 只用于 loopback 上的明文 HTTP；正式 HTTPS/WSS 部署不能
设置它。

Web C 进入 Docker A 的 Term 默认使用 Bash。如需改用 POSIX sh，在上述
`docker run` 命令中追加：

```bash
--env TERMFLOW_SHELL=sh
```

`TERMFLOW_SHELL` 只接受 `bash` 和 `sh`。修改后需要保留身份和工作目录，并
重新创建 Docker A 容器；新的 tmux pane 使用所选 shell，已经运行的 pane 不会热切换。

进入 Docker A 的 Term、查看状态和排障：

```bash
docker exec --user termflow -it termflow-node termflow attach demo
docker exec --user termflow termflow-node termflow doctor
docker exec --user termflow termflow-node termflow status demo --json
docker logs termflow-node
```

`docker exec` 必须带 `--user termflow`：A 以容器内 UID 1000 运行，并为每个 Instance 使用独立
tmux socket；以 root 执行或直接运行 `tmux list-sessions` 只会看到默认 socket 并报
“No such file or directory”。`termflow status demo --json` 会返回该 Instance 的精确
`socket_path`，只有在必须裸用 tmux 时才用 `tmux -S <socket_path>`。注册码过期时
`docker logs` 会显示注册失败，重新生成注册码并保留身份目录重试即可。

`termflow-node-identity/` 保存 A 的身份，`termflow-node-work/` 保存用户文件。容器首次启动会
把这两个目录改成容器内 `termflow`（UID 1000）所有；删除目录会删除对应数据。

## 客户端

正式构建版本的解析顺序固定为
`Git Tag > TERMFLOW_BUILD_VERSION > 0.2.0-dev.0`。Tag Release 直接使用 `vX.Y.Z`
（以及受支持的 prerelease/build metadata）中的版本；手动 workflow 可填写可选版本，本地构建可设置
`TERMFLOW_BUILD_VERSION=1.2.3`。两者都没有时使用明确的开发版本
`0.2.0-dev.0`，不会被误认为正式 Release。Web C 已包含在 Control Plane 镜像中。Windows、Linux、macOS、Android 和 iOS Simulator
客户端从 [GitHub Releases](https://github.com/mcocdaa/TermFlow/releases) 下载。

## 更新与备份

使用新的精确 tag 重建容器即可升级。升级或回退前备份 B 的 `data/`、`totp-secrets/`，
以及 A 使用的 Docker volumes。不要使用 `latest`，也不要在升级时删除数据卷。

详细说明见 [部署与恢复](docs/operations.md)。

## 技术架构

| 组件 | 技术 | 职责 |
| --- | --- | --- |
| Computer A | Python 3.12、tmux、asyncio、WebSocket | 运行 Term、管理 tmux，并主动连接 B |
| Control Plane B | FastAPI、Uvicorn、SQLAlchemy 2、Alembic、SQLite | 认证、Computer 注册、状态同步和终端流量转发 |
| Web C | Vue 3、TypeScript、Vite、xterm.js | 浏览器和手机端的终端与管理界面 |
| 原生 C | Tauri 2、Rust，共用 Vue UI | Windows、Linux、macOS、Android 和 iOS Simulator 客户端 |
| Protocol | Pydantic 2 | A、B 之间的版本化消息协议 |

A 保存并执行真实终端进程；B 不执行 A 的 shell 命令，只负责认证、路由和转发。Web C
由 B 的同一镜像提供，原生客户端复用同一套客户端核心、UI 和协议契约。

## 开发与文档

从源码构建、开发环境和 Release 流程不放在安装入口中：

- [部署与恢复](docs/operations.md)
- [架构与进程边界](docs/architecture.md)
- [协议](docs/protocol.md)
- [API 示例](docs/api-examples.md)
- [Web C](docs/web-client.md)
- [安全与隐私](docs/security.md)
- [排障](docs/troubleshooting.md)
- [构建与发布](docs/github-actions.md)
- [设计与实施记录](docs/superpowers/README.md)
