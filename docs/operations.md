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

### 日志收集

B 不创建容器内应用日志文件；请使用 `docker compose --env-file .env -f deploy/compose.yaml logs -f control-plane`，
并由容器平台负责保留与轮转。A/Tauri 的本地日志位置见 [README](../README.md#日志位置)；Web C 不写磁盘日志，
用响应头 `X-Request-ID` 在 B 输出中关联请求。

Windows 上 A 的日志是 `%LOCALAPPDATA%\\termflow\\Logs\\termflow.log`；Tauri 的
`termflow-client.log` 位于系统应用日志目录。需要新版原生授权或网络能力时，从 Tag Release
或 `Package C · Native Clients` 下载新的未签名 Windows NSIS 包并覆盖安装；旧安装包不会自动
替换为新代码。Tauri 的“在其他设备上授权”设备码有效 15 分钟，使用已登录的 Web C 确认；
Admin Token 仍只用于 Web C 登录。

## 永久发布、安装与回退

一个通过全部介质 gate 的 `vX.Y.Z` 或 prerelease tag 会创建 GitHub Release，并推送同 tag 的 GHCR
`ghcr.io/<repository-owner>/termflow-control-plane:<tag>` 多架构镜像（linux/amd64、linux/arm64）。稳定 tag
还会更新 `latest`，但生产部署应始终写精确 tag，不应依赖 `latest`。GitHub Release 中包含 A 的
`install-termflow-node.sh` 与 Linux Node bundle，以及 Windows、Linux、macOS、Android 和 iOS
Simulator 客户端；发布前必须确认 GHCR 包已对目标部署者可拉取。

Computer A 的 Linux x86_64 安装命令是：

```bash
curl -fsSL https://github.com/mcocdaa/TermFlow/releases/download/vX.Y.Z/install-termflow-node.sh | bash
```

安装器先下载 archive 与 `SHA256SUMS`、校验 checksum，再原子更新当前用户的
`~/.local/bin/termflow` 符号链接。它要求 tmux 3.2+，不使用 `sudo`、不创建 systemd 服务、
不删除旧版本，也不应输出任何注册码或 token。上面的 URL 是官方仓库；Fork 发布时替换为
自己的 owner/repository。回退 A 时重新运行旧 tag 的安装命令即可。

B + Web C 的默认 Compose 从当前 checkout 构建，不绑定 GitHub 所有者、Registry 或镜像 tag。
部署前切换到已验证的精确源码 tag 或 commit，然后运行：

```bash
cp .env.example .env
# 编辑 TERMFLOW_ADMIN_TOKEN 和实际的 TERMFLOW_PUBLIC_BASE_URL。
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

回退 B 时切换到已验证的旧源码 tag 或 commit，重复 `up -d --build`；不要执行 `down --volumes`，
也不要删除 `termflow-data`。GitHub Actions 或 Fork 可以使用同一个 Dockerfile 构建、标记和发布镜像，
但镜像来源不属于普通 Compose 的运行时配置。

如果要直接运行正式 GHCR 镜像，镜像地址必须由部署者明确选择；它不是 `.env` 的必填项，也不会
被默认 Compose 隐式替换：

```bash
set -a; source .env; set +a
docker run -d --name termflow-control-plane --restart unless-stopped \
  --publish "127.0.0.1:${TERMFLOW_HOST_PORT:-8765}:8000" \
  --volume "${TERMFLOW_DATA_VOLUME:-termflow-data}:/app/data" \
  --env TERMFLOW_ADMIN_TOKEN="$TERMFLOW_ADMIN_TOKEN" \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_PUBLIC_BASE_URL="$TERMFLOW_PUBLIC_BASE_URL" \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK="${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}" \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/data/totp-master-key \
  ghcr.io/<repository-owner>/termflow-control-plane:vX.Y.Z
```

这个 `docker run` 与 Compose 使用同一个数据卷时，不要同时启动两个 B 实例占用
同一个 SQLite 文件；切换镜像前先停止旧容器，再保留卷并启动新容器。启动后检查：

```bash
curl -fsS http://127.0.0.1:8765/healthz
```

Actions artifact 是短期测试产物：三套手动打包 workflow 的产物都保留 14 天，不能代替
GitHub Release。Windows asset 目前未签名；iOS Simulator asset 只能用于 Simulator，不能用于实体
iPhone。签名、notarization、TestFlight 与应用商店上传仍是后续独立流程。

## 管理凭据与 TOTP 密钥

`TERMFLOW_ADMIN_TOKEN` 必须由部署者生成。默认单实例 Compose 会在持久化的
`termflow-data` 数据卷中自动创建权限为 `0600` 的 TOTP 主密钥文件，并在后续启动中复用；
密钥不会进入镜像、日志或仓库。显式设置的 `TERMFLOW_TOTP_MASTER_KEY` 或
`TERMFLOW_TOTP_MASTER_KEY_FILE` 始终优先于这个自动文件。

从仓库根目录运行 Compose 时请显式指定根目录的 env 文件；因为 Compose 文件位于
`deploy/`，不指定时 Compose 可能把 `deploy/` 作为 project directory，进而找不到根目录
的 `.env`。首次部署先复制示例，再按实际入口修改；使用反向代理时，只需把
`TERMFLOW_PUBLIC_BASE_URL` 填写为公网 HTTPS origin。B 默认使用同一个 origin 校验浏览器请求：

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

多 B 实例不能各自使用自动文件；部署者必须生成同一个 32 字节无填充 base64url 密钥，并把
同一个显式密钥安全注入所有 B：

```bash
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

不要把输出提交到 `.env` 示例、镜像层、CI 日志或版本控制。需要显式文件注入时，使用仓库提供的
只读 Compose override；路径变量指向宿主机上权限受限且只包含该 base64url 值的文件：

```bash
TERMFLOW_TOTP_MASTER_KEY_FILE=/secure/termflow-totp-key \
  docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.totp-secret.yaml up -d
```

override 把文件挂载到 `/run/secrets/termflow-totp-master-key`，并设置 B 的
`TERMFLOW_TOTP_MASTER_KEY_FILE`。默认自动文件只适用于 Compose 的单实例数据卷；密钥丢失
不能通过 Web C、App 或 EXE 恢复。

验证器丢失时，拥有 Docker 主机权限的管理员在容器内执行本地恢复命令：

```bash
docker compose --env-file .env -f deploy/compose.yaml exec control-plane termflow-control auth totp reset
```

该命令属于 B 的容器内管理面，不是远程 HTTP API。执行前应备份 metadata 数据卷，并按命令
提示确认；重置会推进认证 epoch，使既有会话和令牌失效，但不会删除已登记的 Computer。

## 构建证明的边界

`scripts/verify.sh` 验证当前主机上的 Python、Web、Rust/Tauri 与 Docker gate。一个 Linux
runner 的成功不能证明 Windows/macOS 可编译，更不能证明任何已签名安装包存在。CI 的桌面
job 在三个原生 runner 上做 `--no-bundle` 无签名编译；Android/iOS job 每次从已提交的同一份
Tauri 配置生成平台工程，再执行 debug/unsigned 编译。缺少 Tauri 工程或生成失败都会使 CI
失败。Control Plane 镜像构建失败时最多尝试 3 次、间隔 10 秒，用于吸收 registry 的瞬时网络
故障；最后一次仍失败时保留原始退出码并使 CI 失败。可通过 `TERMFLOW_DOCKER_BUILD_ATTEMPTS` 和
`TERMFLOW_DOCKER_BUILD_RETRY_DELAY_SECONDS` 调整。签名、notarization、商店上传和发布凭据
属于单独受保护的 release 流程。

## 三套手动打包 Workflow 与正式 Release

各 workflow 的文件名、输入、Artifact 名称和 Tag 依赖图见
[GitHub Actions 构建与发布](github-actions.md)；以下只保留部署时需要的版本与验收边界。

基础打包文件同时声明 `workflow_dispatch` 和 `workflow_call`：前者供 Actions 页面手动测试选定的
branch/tag ref，后者只供 Tag Release 复用同一套命令。若要固定某个 commit，先创建临时
branch/tag。手动运行不接受 `release_tag`，不会创建 GitHub
Release，也不会推送 GHCR；Actions artifact 使用不含版本的稳定名称并保留 14 天。三个手动
表单都可以填写可选 `version`；留空时读取仓库 Actions 变量 `TERMFLOW_BUILD_VERSION`，仍未设置
则使用 `0.0.1-dev.0`。

统一版本优先级为 `Git Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0`。本地非 Tag 构建可显式运行：

```bash
TERMFLOW_BUILD_VERSION=1.2.3 \
  python scripts/release/prepare_version.py --resolve-only
```

环境变量只能决定非 Tag 构建包内的版本，不能触发 GHCR 或 GitHub Release，也不能覆盖 Tag。
非法的非空版本会直接失败，不会静默使用默认值。

完整 SemVer 是产品逻辑版本。Android 使用由数字 core 派生的正整数 `versionCode`，macOS/iOS
平台包使用纯数字 core；原生客户端授权元数据仍上报完整逻辑版本。当前 deb 仅作为手动下载
Artifact，prerelease 的 Debian 版本排序不作为 apt 升级通道承诺。如果 Tag 含
`+build-metadata`，GHCR 不能使用原始加号，workflow 会把它映射为下划线（例如
`v1.2.3+build.5` → `v1.2.3_build.5`）；GitHub Release 文件名仍保留原始 Tag。

- `Package A · Linux Node` 构建 Linux x86_64 A bundle、安装器和 `SHA256SUMS`。下载并解压
  `termflow-node-linux-x86_64` Artifact 后，可在该目录离线安装：

  ```bash
  TERMFLOW_RELEASE_BASE_URL="$(python3 -c 'from pathlib import Path; print(Path.cwd().as_uri())')" \
    ./install-termflow-node.sh
  ```

  使用 `Path.cwd().as_uri()` 是为了正确编码 Windows 下载目录中的空格和括号。

- `Package B + Web C · Control Plane` 构建、启动验证并重新导入 amd64 Docker 镜像。下载并
  解压 `termflow-control-plane` Artifact 后，可导入本机 Docker：

  ```bash
  docker load -i termflow-control-plane.tar
  ```

  手动导入不会覆盖 Compose 配置、重启服务或删除 `termflow-data`；部署者应显式选择镜像运行方式。
- `Package C · Native Clients` 的 `platform` 可选 `all`、`windows`、`linux`、`macos`、
  `android` 或 `ios`。Artifacts 分别包含 Windows NSIS `*-setup.exe`、Linux deb/AppImage、
  macOS app zip/DMG、Android debug APK 和 iOS simulator app zip。

手动验证时，把目标 commit 推送到 GitHub，打开上述 workflow 的 Run workflow，等待所选 job
成功后下载 Artifact，并在目标平台实际解包、安装和启动。workflow 成功本身不等于安装验收通过。
历史上的单平台 `Tauri Windows Installer` / `termflow-windows-installer` 入口和 7 天 artifact
规则已被废弃。

正式 Release 不再要求发布者手动同步修改 Python、npm、Cargo 与 Tauri 清单。先在目标 commit
上验证 Tag 格式：

```bash
python scripts/release/prepare_version.py --tag vX.Y.Z --resolve-only
git tag vX.Y.Z
git push origin vX.Y.Z
```

推送 `vX.Y.Z` Tag 后，三个基础 workflow 会在各自 runner 的临时 checkout 中把该版本注入
Python、npm、Cargo、Tauri 和本地锁文件；这些临时修改不会提交回仓库。`Publish TermFlow Release`
先并行调用 A 与原生 C 基础 workflow；
二者全部成功后才调用 B + Web C workflow 并授予其 GHCR `packages: write`。B 的镜像内容、健康
启动、tar round-trip、Artifact 上传或 multi-arch 推送任一步失败，最终 Release job 都不会运行。
三套基础 workflow 全部成功后，Release job 才合并产物、重新生成唯一的 `SHA256SUMS` 并创建
GitHub Release。Tag 中间 Artifact 名含完整 Tag、保留 1 天；永久文件由 GitHub Release 保存。
不要把手动测试 artifact 当成正式发布，也不要在未经验证的 Tag 上重复发布。

这些产物的信任和签名边界如下：

- Windows NSIS 没有发布者代码签名，SmartScreen 显示“未知发布者”属于预期。公开分发需要独立的
  Windows 代码签名和受保护凭据。
- Linux deb/AppImage 没有发行签名；AppImage 使用 Ubuntu 22.04 作为兼容性构建基线。
- macOS app 使用 ad-hoc identity 打包，DMG 未做 Developer ID notarization；下载后仍可能需要用户
  在 Privacy & Security 中明确放行。公开分发需要 Developer ID、notarization 和 stapling。
- Android 是可安装的 debug APK。它由 Gradle debug keystore 签名，而不是“完全无签名”，也不是
  稳定的生产签名；不同 run 之间可能无法覆盖升级。Google Play 发布需要受保护的长期 upload key、
  release signing 配置、递增 versionCode 和正式 AAB/APK。
- iOS zip 内是 `aarch64-sim` simulator `.app`，只能安装到匹配架构的 iOS Simulator，不能安装到
  物理 iPhone。物理设备、TestFlight 或 App Store 需要 Apple Developer team、证书、provisioning
  profile、entitlements 和受保护的签名流程。

Control Plane Docker 镜像不包含这个安装包，也不包含 Tauri、Rust、NSIS 或任何桌面/移动构建
工具链。

也可以直接在原生 Windows 主机安装 Rust stable MSVC、Visual Studio C++ Build Tools、WebView2、
Node 22.23.2 和 npm，然后在仓库根目录执行：

```powershell
npm ci
npm run tauri:build --workspace @termflow/tauri-client -- --bundles nsis
```

这条本地命令同样只生成未签名 NSIS 测试包；WSL 环境不能代替原生 Windows 打包证明。
