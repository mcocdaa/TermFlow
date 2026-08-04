# 排障指南

所有检查默认只读，不要用默认 tmux socket 猜测 Instance。

## 基础检查

```bash
python --version
tmux -V
uv run --package termflow-node termflow doctor
uv run --package termflow-node termflow list --json
```

要求 Python 3.12、tmux 3.2+。`termflow doctor --repair` 只会修复已知 TermFlow 文件权限，
并在 tmux 仍存活时重启缺失 Bridge；它不会删除状态、杀 tmux 或执行远端命令。

## Release 安装器或升级失败

正式 Linux A 使用 GitHub Release 的 `install-termflow-node.sh`。手动 A workflow 的 Artifact
也包含同一安装器、bundle 和中间 `SHA256SUMS`，但只保留 14 天；先确认安装器对应的精确版本与
本机版本：

```bash
termflow --version
tmux -V
```

安装器要求 Linux x86_64、tmux 3.2+、`curl` 与 `sha256sum`。出现 SHA256/checksum 失败时不要绕过
校验：它会在更新 `~/.local/bin/termflow` 前失败，因此原有可用版本仍会保留。检查 Release 的
`SHA256SUMS`、网络代理和 tag 后重试；需要回退时运行旧 Release tag 的安装器。

手动 A 的离线安装失败时，确认已把 Artifact 完整解压到同一目录，并从该目录执行：

```bash
TERMFLOW_RELEASE_BASE_URL="$(python3 -c 'from pathlib import Path; print(Path.cwd().as_uri())')" \
  ./install-termflow-node.sh
```

不要单独移动安装器、bundle 或 `SHA256SUMS`，也不要删除校验步骤。

如果手动包显示了意外版本，先检查 Actions 表单中的 `version` 和仓库变量，或本地 shell 中的
`TERMFLOW_BUILD_VERSION`。解析顺序固定为
`Git Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0`；无 Tag、无环境变量的包显示
`0.0.1-dev.0` 是预期行为。正式 Tag 构建不会被同名环境变量覆盖。

手动 B 的 `termflow-control-plane.tar` 无法导入时，先确认 Artifact 已完整解压且 Docker daemon
可用，再运行 `docker load -i termflow-control-plane.tar`。导入只把镜像加入本机，不会自动更新
Compose 服务；不要用 `docker system prune` 排障，也不要删除 `termflow-data`。

如果 B/Web C 更新后需要回退，切换到已验证的旧源码 tag 或 commit，再运行
`docker compose --env-file .env -f deploy/compose.yaml up -d --build`。不要为了回退执行
`docker compose down --volumes`，那会删除 metadata 数据卷。

## App/EXE 显示“无法连接服务器”

先在同一台机器确认 B 的健康检查，而不是先重复输入管理员 Token：

```bash
curl -fsS "http://127.0.0.1:${TERMFLOW_HOST_PORT:-8765}/healthz"
```

上面的 `8765` 是 Compose 默认端口；如果 `.env` 修改了 `TERMFLOW_HOST_PORT`，使用修改后的
端口。远程部署则应从反向代理的 HTTPS 服务网址检查，而不是访问本机 loopback。

Tauri 的服务器地址必须是没有路径、查询串或片段的完整 origin，例如
`https://relay.example.com` 或本机 `http://127.0.0.1:8765`。它必须与 B 的
`TERMFLOW_PUBLIC_BASE_URL` 完全一致；反向代理、证书和 WSS 必须从系统浏览器也能访问。

点击“申请注册远程控制”后，App/EXE 会打开系统浏览器，而不是在本机表单中填写管理员
Token。浏览器完成授权并回到 `termflow://auth/callback` 后，原生客户端才会得到 token。
确认 Web C 已使用同一服务网址、浏览器没有拦截回调，并检查 B 日志中的授权失败原因。

旧的 Windows 安装包不会自动包含新代码；如果本地包在 IPv4 可用时仍报告网络离线，重新从
Tag Release 或 `Package C · Native Clients` workflow 下载新的 Windows NSIS 包。当前版本对
`127.0.0.1`、`localhost` 和 `[::1]` 的 HTTP capability 均有解析/匹配契约；“服务器不可用”
与“客户端网络权限配置无效”是两种不同错误。

## GitHub Actions 失败或找不到包

- 手动 workflow 的 Artifact 只保留 14 天；过期后重新运行对应 workflow，不能从仓库源码页面
  直接下载旧 Artifact；
- Tag workflow 的中间 Artifact 只保留 1 天，永久文件在 GitHub Release；
- Tag 的任一 A/C/B job 失败时，`publish` 不会运行，因此没有 GitHub Release，也不会把 B 镜像
  推到 GHCR。先修复失败 job，再从同一个 workflow 重新运行或使用新的 Tag；
- Docker base image、Debian 包或 crates.io 下载超时时，先看失败 URL 和 runner 网络。B 镜像脚本
  默认最多重试 3 次；CI 的瞬时 registry 502 不代表应用代码已通过镜像 gate；
- CI 的无签名 compile 只证明对应 runner 能编译，不能替代手动 C workflow 的安装包 Artifact。

完整 workflow 输入、Artifact 名称和 Tag 依赖见 [GitHub Actions 构建与发布](github-actions.md)。

## Instance 显示 bridge-down

先确认 B `/healthz`、A 的网络与服务器 URL。B 重启后 Bridge 会自动重连；tmux 始终可用：

```bash
uv run --package termflow-node termflow attach '<exact-instance-uuid>'
```

如果凭据被吊销，重新登录/注册，不要把 token 放入 URL 或日志。Instance Credential 单独
吊销不会影响同一电脑的其他 Instance。

## topology 不可用或 Pane 不存在

拓扑只代表当前在线状态，B 不伪装离线前的快照。先等 Bridge 完成重连与完整快照，再用
API 返回的 `%<digits>` Pane ID。Pane 在校验和输入之间消失会得到 `pane_not_found`。

## 安全停止

先用 `termflow list --json` 获得精确 UUID，再执行：

```bash
uv run --package termflow-node termflow kill '<exact-instance-uuid>'
```

这只停止对应 Bridge 与显式 tmux server。不要运行宽泛的 `pkill tmux`，也不要删除整个
TermFlow 状态根目录。tmux detach 只是离开本地界面，不会停止 Instance。
