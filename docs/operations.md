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

从仓库根目录运行 Compose 时请显式指定根目录的 env 文件；因为 Compose 文件位于
`deploy/`，不指定时 Compose 可能把 `deploy/` 作为 project directory，进而找不到根目录
的 `.env`。首次部署先复制示例，再按实际入口修改；使用反向代理时，
`TERMFLOW_PUBLIC_BASE_URL` 和 `TERMFLOW_TRUSTED_WEB_ORIGINS` 应填写同一个公网 HTTPS origin：

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
  docker compose -f deploy/compose.yaml -f deploy/compose.totp-secret.yaml up -d
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

## 多平台 Tauri 测试包

`Tauri Multi-platform Packages` workflow 可以在 Actions 页面手动触发，也会在推送 `v*` tag
时触发。旧的 `Tauri Windows Installer` 和 `termflow-windows-installer` 单平台入口已经被该
workflow 取代；旧 artifact 的 7 天保留期不再适用。手动运行会保留 14 天，tag 运行会保留 90 天。

手动验证任意 commit：

1. 把需要测试的 commit 推送到 GitHub。
2. 打开 Actions → `Tauri Multi-platform Packages` → Run workflow，选择对应分支或 tag，再在
   `platform` 中选择 `all`、`windows`、`linux`、`macos`、`android` 或 `ios`。选择单个平台时，
   其他平台 job 会跳过；选择 `all` 时运行全部五个平台。
3. 等待版本校验和所选原生打包 job 成功。
4. 从该次 run 的 Artifacts 下载所选产物：Windows NSIS `*-setup.exe`、Linux deb/AppImage、macOS app zip/DMG、
   Android debug APK 和 iOS simulator app zip。
5. 对需要声明支持的平台实际解包、安装并启动；workflow 成功本身不等于安装验收通过。

创建 tag 前，先把根 `package.json`、Tauri client `package.json`、`src-tauri/Cargo.toml` 和
`src-tauri/tauri.conf.json` 的版本同步为同一个 SemVer，合并到目标 commit，再显式推送匹配的
`v<version>` tag。tag 与配置版本不一致或 tag 不是合法的 `v` 前缀 SemVer 时，workflow 会在
任何原生构建开始前失败。tag 触发不接受平台筛选，始终构建全部五个平台；workflow 只上传
受保留期约束的 Actions artifacts，不创建或更新
GitHub Release，也不上传商店。

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
