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

正式 Linux A 使用 GitHub Release 的 `install-termflow-node.sh`，不是 Actions artifact。先确认
下载的精确 tag 与本机版本：

```bash
termflow --version
tmux -V
```

安装器要求 Linux x86_64、tmux 3.2+、`curl` 与 `sha256sum`。出现 SHA256/checksum 失败时不要绕过
校验：它会在更新 `~/.local/bin/termflow` 前失败，因此原有可用版本仍会保留。检查 Release 的
`SHA256SUMS`、网络代理和 tag 后重试；需要回退时运行旧 Release tag 的安装器。

如果 B/Web C 更新后需要回退，切换到已验证的旧源码 tag 或 commit，再运行
`docker compose --env-file .env -f deploy/compose.yaml up -d --build`。不要为了回退执行
`docker compose down --volumes`，那会删除 metadata 数据卷。

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
