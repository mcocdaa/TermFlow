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

## 永久发布、安装与回退

一个通过全部介质 gate 的 `vX.Y.Z` tag 会创建永久 GitHub Release，并推送同 tag 的 GHCR
`ghcr.io/mcocdaa/termflow-control-plane:vX.Y.Z` 多架构镜像（linux/amd64、linux/arm64）。稳定 tag
还会更新 `latest`，但生产部署应始终写精确 tag，不应依赖 `latest`。GitHub Release 中包含 A 的
`install-termflow-node.sh` 与 Linux Node bundle，以及 Windows、Linux、macOS、Android 和 iOS
Simulator 客户端；发布前必须确认 GHCR 包已对目标部署者可拉取。

Computer A 的 Linux x86_64 安装命令是：

```bash
curl -fsSL https://github.com/mcocdaa/TermFlow/releases/download/vX.Y.Z/install-termflow-node.sh | bash
```

安装器先下载 archive 与 `SHA256SUMS`、校验 checksum，再原子更新当前用户的
`~/.local/bin/termflow` 符号链接。它要求 tmux 3.2+，不使用 `sudo`、不创建 systemd 服务、
不删除旧版本，也不应输出任何注册码或 token。回退 A 时重新运行旧 tag 的安装命令即可。

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

Actions artifact 是短期测试产物：手动 `Tauri Multi-platform Packages` run 保留 14 天，不能代替
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

## 多平台客户端测试包与正式 Release

`Tauri Multi-platform Packages` workflow 仅可在 Actions 页面手动触发，用来测试任意 commit；
它绝不创建 GitHub Release、绝不推 GHCR 版本镜像，artifact 保留 14 天。历史上的单平台
`Tauri Windows Installer` / `termflow-windows-installer` 入口和 7 天 artifact 规则已被废弃。

手动验证任意 commit：

1. 把需要测试的 commit 推送到 GitHub。
2. 打开 Actions → `Tauri Multi-platform Packages` → Run workflow，选择对应分支或 tag，再在
   `platform` 中选择 `all`、`windows`、`linux`、`macos`、`android` 或 `ios`。选择单个平台时，
   其他平台 job 会跳过；选择 `all` 时运行全部五个平台。
3. 等待版本校验和所选原生打包 job 成功。
4. 从该次 run 的 Artifacts 下载所选产物：Windows NSIS `*-setup.exe`、Linux deb/AppImage、macOS app zip/DMG、
   Android debug APK 和 iOS simulator app zip。
5. 对需要声明支持的平台实际解包、安装并启动；workflow 成功本身不等于安装验收通过。

准备正式 Release 前，先让根 `package.json`、Node、Control Plane、protocol、Tauri client 的
`package.json`、`src-tauri/Cargo.toml` 与 `tauri.conf.json` 全部为同一 SemVer，并在目标 commit
上验证：

```bash
uv run --frozen python scripts/release/check_version.py --tag vX.Y.Z
```

推送匹配的 `vX.Y.Z` tag 后，`Publish TermFlow Release` 会始终构建 A、B 和全部五类 C 包；A 的
已安装终端连通 B、B 的 release-image smoke、每种原生包的文件数检查任一失败，发布 job 都不会
得到 `contents: write` / `packages: write` 权限。全部成功后才上传 Release assets、`SHA256SUMS` 和
GHCR multi-arch image。不要把手动测试 artifact 当成正式发布，也不要在未经验证的 tag 上重复发布。

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
