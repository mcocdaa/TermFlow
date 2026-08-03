# TermFlow 安装与发行链路设计

- 日期：2026-08-03
- 状态：已确认，待实施
- 范围：A Linux Node 安装、B + Web C Docker 镜像、Windows/iOS 等 C 原生包，以及 GitHub Actions 的测试与正式发行交接
- 不包含：代码签名、macOS notarization、iOS 真机/TestFlight、应用商店上传、自动部署、远程升级或重新设计 A/B/C 协议

## 1. 目标与原则

TermFlow 的永久交付物由 GitHub Release 和 GitHub Container Registry（GHCR）承担，不能依赖有保留期且可能需登录的 Actions artifact。每个发布 tag 都必须对应可复现、可下载的 A/C 安装介质和 B + Web C 镜像；分支 CI 只验证构建与安装，不发布任何永久资产。

发布版本统一使用 `vX.Y.Z` 或 `vX.Y.Z-rc.N`：同一 tag 必须与根 `package.json`、Tauri 配置、Node、Control Plane 和 protocol 的版本一致。稳定版才更新 Docker 的 `latest`；预发布仅提供精确版本资产与镜像标签。

## 2. A：Linux Node 的用户级安装

A 首发支持 `linux/x86_64`，采用在 Ubuntu 22.04 构建的 PyInstaller one-directory 原生 bundle：

```
termflow-node-linux-x86_64.tar.gz
├── termflow/
│   ├── termflow
│   └── _internal/
└── VERSION
```

它包含 Python 运行时和 Python 依赖，但不包含 `tmux`。安装脚本在该 tag 的 Release assets 中，用户执行：

```bash
curl -fsSL https://github.com/mcocdaa/TermFlow/releases/download/vX.Y.Z/install-termflow-node.sh | bash
```

脚本只在当前用户目录写入：下载同 tag 的 archive 和 `SHA256SUMS`、验证 archive 哈希、原子解压到 `~/.local/lib/termflow/vX.Y.Z/`，再将 `~/.local/bin/termflow` 原子指向新版本。安装失败必须留下原版本；重复执行应幂等。它检查 `tmux >= 3.2` 和可用的 `sha256sum`，在缺失时退出并提供系统包管理器提示；不执行 sudo、不修改 shell 配置、不安装 Python/uv、不记录 enrollment 或 installation secret。

A 不创建 systemd 常驻服务。现有模型由每个 `termflow new` 创建私有 tmux Term，并让该 Term 的独立 Bridge 自行连接和重连 B；机器重启后 tmux Term 本身不存在，空转的全局 daemon 既不能恢复它也无可管理对象。PyInstaller bundle 需要让 Bridge 以同一 bundle executable 的 `_bridge` 子命令启动，不能继续假设 `sys.executable -m termflow_node` 在冻结应用中有效。

后续增加 `linux/arm64` 时，产物命名、校验和、安装路径和 installer 协议不变，只增加架构选择；它不阻塞 x86_64 交付。

## 3. B + Web C：版本化 Docker 部署

现有多阶段 Dockerfile 保持 B 和 Web C 同镜像、运行时无源码/Node/Rust/A 的边界。正式 tag 构建并推送：

- `ghcr.io/mcocdaa/termflow-control-plane:vX.Y.Z`；
- `ghcr.io/mcocdaa/termflow-control-plane:sha-<commit>`；
- 仅稳定 tag：`ghcr.io/mcocdaa/termflow-control-plane:latest`。

首版发布 `linux/amd64` 与 `linux/arm64` manifest。镜像带 OCI `source`、`version`、`revision` 标签，Compose 生产模板使用精确 `vX.Y.Z` image 引用而非本地 `build:`。源码开发使用单独的 Compose override 保留本地 build，避免生产部署随工作树改变。Release 同时上传生产 Compose 模板、环境变量示例和 `SHA256SUMS`；服务器管理员仍手动执行 `docker compose pull`、`docker compose up -d`，持久化 `termflow-data` 不变。

首次 GHCR 发布前，仓库 owner 需要把容器包设置为与开源仓库一致的可见性；工作流仅使用 `GITHUB_TOKEN` 的 `packages: write`，不引入服务器凭据或自动部署权限。

## 4. C：桌面与移动包

保留现有原生 runner 构建矩阵，并在 tag 发行时把它们从短期 Actions artifact 提升为 GitHub Release assets：

- Windows x64：未签名 NSIS `*-setup.exe`；
- Linux x64：Tauri `deb` 与 AppImage；
- macOS arm64：ad-hoc `app.zip` 与 DMG；
- Android arm64：debug keystore 签名 APK；
- iOS arm64 Simulator：未签名 `.app.zip`。

iOS asset 的名称和 Release notes 必须明确标为 Simulator，不可描述为真机安装包。签名、notarization、TestFlight 和应用商店属于后续独立安全/发行项目；它们不改变本次 artifact 结构。

## 5. 工作流与原子性

保留 PR/main CI 作为源码质量门禁。新增或重构 tag-release 工作流按以下顺序执行：

1. 验证 tag SemVer、所有产品版本一致性、tag 所在提交已通过常规 CI；
2. 并行构建 A bundle、五种 C 包，并将中间结果保存为 run 内 artifact；
3. 在独立干净环境对 A 运行实际安装测试；对 B + Web C 构建、Compose 启动和健康/SPA/API 测试；
4. 仅在所有构建和测试成功后，构建并推送 GHCR 双架构镜像；
5. 创建 GitHub Release（预发布 tag 使用 prerelease）并上传 A、C、Compose、env 示例和聚合 `SHA256SUMS`。

Release 创建是最后的用户可见步骤，故失败的构建不会留下部分下载资产。镜像推送后若 Release API 失败，workflow 必须失败并报告精确 immutable image tag，由维护者修复后重新运行发布 job；不得悄悄将不完整状态称为发布完成。

手动 workflow_dispatch 继续产出有期限的测试 artifacts，允许选择 A、B 或各 C 平台；它没有 `contents: write` / `packages: write`，不会建立 Release 或推送版本镜像。

## 6. 验收与回归测试

- A 打包测试在干净 Ubuntu 22.04 环境中执行 installer，检查无 Python/uv/源码依赖、用户级路径、哈希校验失败、可重复安装、升级原子性、`termflow --help` 和 `termflow doctor` 的 tmux 诊断。
- A 的已安装二进制对隔离 B 完成 `login -> new -> Bridge online -> remote terminal`，证明冻结 bundle 的 `_bridge` 子进程路径正确。
- B 镜像测试继续检查 source-free runtime；以生产 image 契约启动 Compose，验证 `/healthz`、Web C SPA、登录及既有终端端到端路径。
- 工作流 contract tests 验证版本源、稳定/预发布 tag 规则、Release asset 完整清单、checksum 文件、GHCR 双架构平台和 OCI 标签；配置变更不应只靠 YAML 视觉审查。
- 每个 C 包 job 保留当前的“必须产出精确文件数”检查。真实 Windows/iOS 设备安装在相应签名方案获批前不作为本次通过条件。

## 7. 运行边界与回滚

Release asset 与镜像均按精确版本永久保留；管理员可将 A installer 指向旧 tag，或将 Compose image 回退到旧 `vX.Y.Z` 后手动 `docker compose up -d`。应用数据卷不随镜像替换删除。installer 不能删除旧版本目录，直到同一用户明确执行未来的卸载/清理命令。

本次仅建立软件供应与安装验证链路；B 的 HTTPS/WSS 反向代理、用户注册、A 本地权限模型、终端内容不持久化、发布签名边界均保持既有约束。
