# 部署与恢复

## Control Plane 容器边界

`deploy/Dockerfile.control-plane` 在独立阶段构建 B 与 protocol wheel、Web C `dist`，最终镜像
只复制安装后的 Python 虚拟环境和 Web 静态文件。Computer A、Tauri 工程、Node/npm、Cargo、
Rust、仓库源码、锁文件和测试都不进入最终 runtime。`scripts/verify-control-plane-image.sh`
会在构建后检查这份文件与工具清单。

Compose 默认只把 B 的 HTTP 端口绑定到宿主机 loopback。DNS、反向代理、TLS 终止和可选
mTLS 的证书签发、校验与轮换不属于 TermFlow，也不会被默认镜像或 Compose 创建。生产环境
应由部署者提供 HTTPS/WSS 入口，并把用户实际访问的 canonical URL 写入
`TERMFLOW_PUBLIC_BASE_URL`；B 不因这个配置而自行提供 TLS。

## 管理凭据与 TOTP 密钥

`TERMFLOW_ADMIN_TOKEN` 必须由部署者生成。默认单实例 Compose 会在持久化的
`termflow-data` 数据卷中自动创建权限为 `0600` 的 TOTP 主密钥文件，并在后续启动中复用；
密钥不会进入镜像、日志或仓库。显式设置的 `TERMFLOW_TOTP_MASTER_KEY` 或
`TERMFLOW_TOTP_MASTER_KEY_FILE` 始终优先于这个自动文件。

多 B 实例不能各自使用自动文件；部署者必须生成同一个 32 字节无填充 base64url 密钥，并把
同一个显式密钥安全注入所有 B：

```bash
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

不要把输出提交到 `.env` 示例、镜像层、CI 日志或版本控制。需要显式文件注入时，使用仓库提供的
只读 Compose override；路径变量指向宿主机上权限受限且只包含该 base64url 值的文件：

```bash
TERMFLOW_TOTP_MASTER_KEY_FILE=/secure/termflow-totp-key \
  docker compose -f deploy/compose.yaml -f deploy/compose.totp-secret.yaml up -d
```

override 把文件挂载到 `/run/secrets/termflow-totp-master-key`，并设置 B 的
`TERMFLOW_TOTP_MASTER_KEY_FILE`。默认自动文件只适用于 Compose 的单实例数据卷；密钥丢失
不能通过 Web C、App 或 EXE 恢复。

验证器丢失时，拥有 Docker 主机权限的管理员在容器内执行本地恢复命令：

```bash
cd deploy
docker compose exec control-plane termflow-control auth totp reset
```

该命令属于 B 的容器内管理面，不是远程 HTTP API。执行前应备份 metadata 数据卷，并按命令
提示确认；重置会推进认证 epoch，使既有会话和令牌失效，但不会删除已登记的 Computer。

## 构建证明的边界

`scripts/verify.sh` 验证当前主机上的 Python、Web、Rust/Tauri 与 Docker gate。一个 Linux
runner 的成功不能证明 Windows/macOS 可编译，更不能证明任何已签名安装包存在。CI 的桌面
job 在三个原生 runner 上做 `--no-bundle` 无签名编译；Android/iOS job 每次从已提交的同一份
Tauri 配置生成平台工程，再执行 debug/unsigned 编译。缺少 Tauri 工程或生成失败都会使 CI
失败。签名、notarization、商店上传和发布凭据属于单独受保护的 release 流程。
