# GitHub Actions 构建与发布

本页描述仓库当前实际存在的 Actions。工作流文件本身是执行事实来源；本页只解释如何
选择入口、读取产物和判断发布是否成功。

## 工作流总览

| 工作流 | 手动运行 | Tag 运行 | 输出 |
| --- | --- | --- | --- |
| [`ci.yml`](../.github/workflows/ci.yml) | 否 | 否 | 测试、类型检查、Web 构建、Docker 镜像检查、各平台无签名编译 |
| [`package-node.yml`](../.github/workflows/package-node.yml) | 是 | 由 Release 复用 | A：Linux x86_64 bundle、安装器、`SHA256SUMS`；Tag 运行还推送 GHCR 多架构 A 镜像 |
| [`package-control-plane.yml`](../.github/workflows/package-control-plane.yml) | 是 | 由 Release 复用 | B + Web C Docker tar；Tag 运行还推送 GHCR 多架构镜像 |
| [`tauri-packages.yml`](../.github/workflows/tauri-packages.yml) | 是 | 由 Release 复用 | C：Windows、Linux、macOS、Android、iOS Simulator |
| [`release.yml`](../.github/workflows/release.yml) | 否 | `v*` Tag | GitHub Release，包含 A/C/B 产物和总 `SHA256SUMS` |

`package-node.yml`、`package-control-plane.yml` 和 `tauri-packages.yml` 同时声明
`workflow_dispatch` 与 `workflow_call`。手动入口和 Tag Release 复用的是同一套构建命令；
`release.yml` 不复制 A、B 或 C 的打包实现。

## 手动打包

在 GitHub 的 **Actions → 目标工作流 → Run workflow** 中选择 branch 或 tag ref。若必须固定
到某个 commit，先为该 commit 建临时 branch/tag，再运行 workflow；三个表单都
有可选的 `version` 输入：

- 留空时先读取仓库 Actions variable `TERMFLOW_BUILD_VERSION`，没有该变量则使用
  `0.0.1-dev.0`；
- 填写时必须是受支持的逻辑 SemVer，例如 `1.2.3` 或 `1.2.3-rc.1`，不能带 `v`；
- 手动运行永远只上传短期 Artifact，不创建 GitHub Release，也不推送 GHCR。

也可以用 GitHub CLI 从仓库根目录触发：

```bash
gh workflow run package-node.yml --ref main -f version=1.2.3
gh workflow run package-control-plane.yml --ref main -f version=1.2.3
gh workflow run tauri-packages.yml --ref main -f platform=windows -f version=1.2.3
gh run list --workflow package-node.yml --limit 1
RUN_ID=123456789
gh run watch "$RUN_ID"
gh run download "$RUN_ID"
```

省略 `version` 即使用默认解析；C 的 `platform` 可选 `all`、`windows`、`linux`、`macos`、
`android`、`ios`。`all` 会并行构建全部平台。手动 Artifact 外层名称固定且保留 14 天：

- A：`termflow-node-linux-x86_64`；文件是 `termflow-node-linux-x86_64.tar.gz`、
  `install-termflow-node.sh`、`SHA256SUMS`；
- B + Web C：`termflow-control-plane`；文件是 `termflow-control-plane.tar`；
- C：`termflow-windows-x64-nsis`、`termflow-linux-x64`、`termflow-macos-arm64`、
  `termflow-android-arm64-debug`、`termflow-ios-simulator-aarch64`，名称取决于所选平台。

手动 B tar 只需要导入本机 Docker：

```bash
docker load -i termflow-control-plane.tar
```

导入不会修改 `deploy/compose.yaml`、不会自动重启服务，也不会触碰 `termflow-data`；源代码
部署仍使用 `docker compose --env-file .env -f deploy/compose.yaml up -d --build`。

原生客户端改动由 `Package C · Native Clients` 打包验证：选择 `windows` 会生成
`Windows x64 · NSIS` 的 `*-setup.exe`，选择 `all` 则构建全部 C 平台。下载新的 Windows installer 后须在
目标 Windows 主机覆盖安装；workflow 成功或 Artifact 下载不代表该主机已经替换旧 App。Tag
Release 复用同一 workflow，并把通过 gate 的 installer 作为 GitHub Release asset 发布。

## Tag 发布

Tag 必须是 `v` 前缀的受支持 SemVer，例如 `v1.2.3`、`v1.2.3-rc.1`。先在目标 commit 上
验证，不要先修改仓库内的版本占位符：

```bash
python scripts/release/prepare_version.py --tag v1.2.3 --resolve-only
git tag v1.2.3
git push origin v1.2.3
```

推送后，`release.yml` 按下面的依赖顺序运行：

```text
validate-version
       ├── package-node (A)
       └── package-clients (C, platform=all)
                 ↓
       package-control-plane (B + Web C)
                 ↓
       publish (GitHub Release)
```

Tag 运行会在每个临时 checkout 中把 Tag 版本写入 Python、npm、Cargo、Tauri 配置和锁文件，
不会把这些临时改动提交回仓库。Tag Artifact 名称带完整 Tag，例如
`termflow-v1.2.3-node-linux-x86_64`；中间 Artifact 只保留 1 天，最终 GitHub Release 资产
永久保存。

只有 A、C、B + Web C 全部成功，`publish` 才会执行。因此任一编译、镜像健康检查、Docker
tar round-trip、Artifact 上传或多架构推送失败，都不会创建 GitHub Release。

稳定 Tag（无 prerelease）推送：

```text
ghcr.io/<仓库所有者>/termflow-control-plane:v1.2.3
ghcr.io/<仓库所有者>/termflow-control-plane:sha-<commit>
ghcr.io/<仓库所有者>/termflow-control-plane:latest
ghcr.io/<仓库所有者>/termflow-node:v1.2.3
ghcr.io/<仓库所有者>/termflow-node:sha-<commit>
ghcr.io/<仓库所有者>/termflow-node:latest
```

prerelease Tag 只推送版本 Tag 和 commit Tag，不更新 `latest`，并以 GitHub prerelease 发布。
若 Tag 含 `+build-metadata`，B workflow 会把 GHCR 标签中的 `+` 替换为 `_`（例如
`v1.2.3+build.5` → `v1.2.3_build.5`）；GitHub Release 仍使用原始 Tag 名称。
生产 Compose 应固定到精确版本或 commit，不依赖 `latest`。

## 供应链签名与校验

- A 与 B 的 GHCR 镜像均以 `--provenance mode=max` 构建并附带 SBOM attestation，发布后由
  `sigstore/cosign-installer` 做 cosign keyless 签名；拉取方可用 `cosign verify` 校验签名。
- `release.yml` 的 publish job 对全部 Release 资产运行 `actions/attest-build-provenance`，
  生成 GitHub Artifact Attestations（build provenance），并在发布前 `cosign verify` 校验
  已推送镜像的签名。
- A 的安装器在装有 GitHub CLI 时用 `gh attestation verify` 校验 archive 的 provenance，
  校验失败拒绝安装；没有 `gh` 时回退为纯 checksum 校验。

## 产物边界

- Windows：未做代码签名的 NSIS 安装包，SmartScreen 的“未知发布者”是预期结果；
- Linux：x86_64 deb 和 AppImage，未做发行签名；
- macOS：arm64 app zip 和 DMG，ad-hoc 签名，未做 Developer ID notarization；
- Android：arm64 debug APK，由 debug keystore 签名，不能作为长期生产签名；
- iOS：arm64 Simulator `.app` zip，只能给匹配架构的 Simulator，不能安装到实体 iPhone；
- A：当前只有 Linux x86_64 PyInstaller bundle；
- B + Web C：一个最小 runtime 镜像，生产镜像不包含 Node、npm、Rust、Cargo、Tauri 或仓库源码。

正式签名、notarization、TestFlight、应用商店上传和生产密钥属于未接入本工作流的独立流程。

## CI 与打包的区别

`ci.yml` 在 push 和 pull request 上运行代码级验证；它会测试客户端/Python、生成契约、做
类型检查和 Web 构建，并在原生 runner 上做无签名编译。CI 通过不等于已生成可下载的 Windows、
Android 或 iOS Release 资产；安装包必须看对应的手动 workflow 或 Tag Release Artifact。

Fork 不需要设置 `TERMFLOW_IMAGE`。默认 Compose 从当前 checkout 构建；Tag workflow 的
GHCR 名称由当前仓库所有者计算，和运行时 `.env` 无关。
