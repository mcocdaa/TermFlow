# TermFlow 可复用打包 Workflow 设计

日期：2026-08-03

## 背景

当前仓库有三套相互重叠的 GitHub Actions 逻辑：`ci.yml` 验证所有产品，
`tauri-packages.yml` 手动打包原生客户端，`release.yml` 又复制一份 A、B 和各平台 C 的打包命令。
这会让手动打包与 Tag 发布逐渐产生参数、工具链、产物路径和安全检查上的差异。

本设计把 A、B + Web C、原生 C 分别收敛为三套基础 workflow。每套基础 workflow
既可在 GitHub Actions 页面手动运行，也可通过 `workflow_call` 被 Tag Release 调用。
Release workflow 只负责编排、失败传播和最终发布，不再复制任何产品打包实现。

## 目标

- 提供可独立手动运行的 A 打包 workflow。
- 提供可独立手动运行的 B + Web C 打包 workflow。
- 保留可按平台或全部手动运行的原生 C 打包 workflow。
- Tag Release 直接调用上述三套基础 workflow，不复制打包命令。
- 手动 Artifact 使用不含版本的默认名称；Tag 调用的 Artifact 名称包含完整 Tag。
- 任一基础打包失败时，不发布 GHCR 镜像，也不创建 GitHub Release。
- 保持 Fork 可用：镜像仓库所有者继续来自 `GITHUB_REPOSITORY_OWNER`，不硬编码原仓库所有者。

## 非目标

- 不增加代码签名、Windows Authenticode、Apple Developer ID、TestFlight 或商店上传。
- 不把 Android debug APK 改为生产签名包。
- 不把 iOS Simulator 包改为实体 iPhone 安装包。
- 不扩展 A 到 Linux ARM64 或原生 Windows ConPTY。
- 不修改 TermFlow 产品版本；当前版本仍为 `0.1.0`。
- 不修改普通源码部署 Compose 的构建边界。

## Workflow 架构

新增或重构为以下基础 workflow：

| 文件 | GitHub 显示名称 | 手动职责 | Release 调用职责 |
|---|---|---|---|
| `.github/workflows/package-node.yml` | Package A · Linux Node | 构建并验证 A 的 Linux x86_64 bundle 和安装器 | 使用 Tag 构建同一产物 |
| `.github/workflows/package-control-plane.yml` | Package B + Web C · Control Plane | 构建、验证并上传可离线导入的 Docker tar | 构建同一 tar，并推送多架构 GHCR 镜像 |
| `.github/workflows/tauri-packages.yml` | Package C · Native Clients | 按 `all/windows/linux/macos/android/ios` 打包 | 使用 `all` 打包全部平台 |

每个基础 workflow 同时声明：

```yaml
on:
  workflow_dispatch:
  workflow_call:
```

`workflow_call` 接收可选的 `release_tag`。原生 C 还接收 `platform`；手动触发时
`platform` 默认 `all`。手动触发不提供 `release_tag`，因此普通用户不能通过手动表单伪造
Tag 发布模式。

`.github/workflows/release.yml` 保留 `push.tags: ["v*"]`，但只包含：

1. 校验 `github.ref_name` 与所有产品版本一致。
2. 并行调用 A workflow 和原生 C workflow，并显式传入 `release_tag`。
3. A 和 C 全部成功后，调用 B + Web C workflow，并授予 GHCR 写权限。
4. 三套基础 workflow 全部成功后，下载它们的 Artifact，生成 `SHA256SUMS`，创建 GitHub Release。

Release workflow 不允许包含 `build_node_bundle.sh`、Tauri build 命令、Docker build 命令或产品产物路径。

## 调用模式与版本验证

三套基础 workflow 共用 `scripts/release/check_version.py`：

- 手动模式只验证所有产品表面使用同一个配置版本。
- Tag 模式额外执行 `--tag <release_tag>`，要求完整的 v-prefixed SemVer 与配置版本一致。
- Release orchestrator 在调用基础 workflow 前也验证一次 Tag，快速阻止错误版本。

完整 Tag 作为输入传递，不能由各 workflow 根据分支名重新推断。这样 Artifact 名称、镜像标签、
Release 名称和安装器版本使用同一份已验证值。

## Artifact 命名

手动模式使用稳定默认名称；Tag 模式在 `termflow-` 后加入完整 Tag：

| 产品 | 手动 Artifact | Tag `v0.1.0` Artifact |
|---|---|---|
| A | `termflow-node-linux-x86_64` | `termflow-v0.1.0-node-linux-x86_64` |
| B + Web C | `termflow-control-plane` | `termflow-v0.1.0-control-plane` |
| C Windows | `termflow-windows-x64-nsis` | `termflow-v0.1.0-windows-x64-nsis` |
| C Linux | `termflow-linux-x64` | `termflow-v0.1.0-linux-x64` |
| C macOS | `termflow-macos-arm64` | `termflow-v0.1.0-macos-arm64` |
| C Android | `termflow-android-arm64-debug` | `termflow-v0.1.0-android-arm64-debug` |
| C iOS | `termflow-ios-simulator-aarch64` | `termflow-v0.1.0-ios-simulator-aarch64` |

Artifact 外层名称变化，实际文件名保持稳定，避免破坏安装器和下载路径：

- `termflow-node-linux-x86_64.tar.gz`
- `install-termflow-node.sh`
- `termflow-control-plane.tar`
- Tauri 当前生成的 NSIS、deb、AppImage、DMG、APK 和 app zip 文件名

手动 Artifact 保留 14 天。Tag 模式的中间 Artifact 保留 1 天；最终文件进入 GitHub Release 后
按 Release 生命周期保存。Release 下载所有基础 workflow 产生的 Artifact 时使用合并目录，要求所有
实际文件名唯一，再统一生成 `SHA256SUMS`。

## A 打包 Workflow

A workflow 只支持 Linux x86_64，并完成以下工作：

1. 安装 Python 3.12、uv 0.11.19 和 tmux。
2. 从锁定 workspace 构建 `termflow-node-linux-x86_64.tar.gz`。
3. 渲染固定 Tag 和当前 `GITHUB_REPOSITORY` 的 `install-termflow-node.sh`；手动模式使用当前配置版本对应的 Tag。
4. 运行现有 bundle 安装与连接验收。
5. 为手动离线安装生成 `SHA256SUMS`，上传 bundle、安装器和校验和。

手动包仍是一个真实版本的 A，不使用 `latest` 或无版本内部目录；“默认名称”只指 Actions Artifact 名称。
下载并解压手动 Artifact 后，可通过
`TERMFLOW_RELEASE_BASE_URL="file://$PWD" ./install-termflow-node.sh` 使用同目录 bundle 和校验和安装。

`render_node_installer.py` 增加显式 repository 参数，安装器模板不再固定指向 `mcocdaa/TermFlow`。
Tag 调用传入当前 `GITHUB_REPOSITORY`，因此 Fork 的安装器默认从 Fork 自己的 Release 下载。
最终 Release 收集 Artifact 后先删除 A 中间校验和，再针对全部 Release 文件重新生成唯一的 `SHA256SUMS`。

## B + Web C 打包 Workflow

B workflow 的共同阶段：

1. 使用 `deploy/Dockerfile.control-plane` 构建当前 checkout。
2. 验证运行镜像不包含源码、Node、Rust、构建清单或其他无关文件。
3. 启动隔离容器和临时数据卷，等待 `/healthz` 成功。
4. 导出 `termflow-control-plane.tar`，并验证该 tar 能被 `docker load` 恢复。
5. 上传对应名称的 Artifact。

手动模式到此结束，不登录 Registry，不推送 GHCR。tar 是 GitHub-hosted Linux runner 构建的
`linux/amd64` 离线镜像。

Tag 模式在共同阶段成功后额外：

- 配置 QEMU 和 Buildx。
- 使用调用方提供的 `packages: write` 登录 GHCR。
- 构建并推送 `linux/amd64,linux/arm64` 镜像。
- 推送 `:<release_tag>` 和 `:sha-<GITHUB_SHA>`。
- 仅稳定 Tag（不含 `-`）更新 `:latest`；预发布 Tag 不更新 `latest`。

镜像地址继续为 `ghcr.io/${GITHUB_REPOSITORY_OWNER}/termflow-control-plane`，因此 Fork 会发布到自己的命名空间。

## 原生 C 打包 Workflow

现有 `tauri-packages.yml` 保留文件路径并增加 `workflow_call`，避免丢失已有 Actions 入口与历史；
其五个平台 job 保持工具链和产物边界：

- Windows x64：未签名 NSIS installer。
- Linux x64：deb 和 AppImage。
- macOS arm64：ad-hoc-signed app zip 和 DMG。
- Android arm64：debug-keystore-signed APK。
- iOS arm64：Simulator-only app zip。

手动触发可选择单个平台或 `all`；Tag Release 总是传入 `all`。每个平台继续严格要求预期文件数量，
避免 glob 为空或产生重复包时仍然成功。

## 权限边界

- workflow 默认只有 `contents: read`。
- A、C、B 的构建/验证/Artifact job 不需要写权限。
- B 的 Tag GHCR 发布 job 才声明 `packages: write`。
- Release 中调用 B 的 job 显式向 reusable workflow 传递 `packages: write`；被调用 workflow 不能自行提升权限。
- 最终 GitHub Release job 使用 `contents: write`，但不使用 `packages: write`。

手动 B workflow 虽与 Tag 模式共用一个文件，但没有 `release_tag`，GHCR 发布 job 会被跳过。

## 失败传播与发布一致性

- Tag 或版本校验失败：不调用任何基础打包 workflow。
- A 或任一 C 平台失败：B Tag workflow 不运行，因此不推送 GHCR。
- B 内容验证、健康检查、tar round-trip、Artifact 上传或 GHCR 推送失败：不创建 GitHub Release。
- 预期文件缺失或数量不正确：对应基础 workflow 失败。
- 最终校验和或 GitHub Release 创建失败：整个 Tag run 失败，不报告发布成功。

GitHub Release 与 GHCR 是两个外部系统，无法形成数据库式原子事务。如果所有打包成功并已推送 GHCR，
但最后的 GitHub Release API 调用失败，workflow 保持失败状态；操作者应对相同 Tag 重跑失败 job，
而不是创建第二个版本 Tag。该边界不允许掩盖为成功。

## CI 边界

`ci.yml` 继续在 push 和 pull request 上执行单元测试、类型检查、Rust/Tauri 编译及 B 镜像验证，
但不上传正式安装包，也不推送 Registry。删除其中已经失效的 `TERMFLOW_IMAGE=...` 环境前缀。

CI 不调用完整基础打包 workflow，避免每次提交产生所有平台安装包和高额构建时间；它验证底层构建脚本、
工作区和 workflow 契约。真正的可下载包由手动基础 workflow 或 Tag Release 产生。

## 测试与验收

本地契约测试必须证明：

- 三个基础 workflow 同时声明 `workflow_dispatch` 和 `workflow_call`。
- Release 只通过 job-level `uses: ./.github/workflows/...` 调用基础 workflow。
- Release 传入完整 Tag，原生 C 传入 `all`，B 在 A 和 C 成功后才运行。
- Release 不包含任何 A、B、C 的产品打包命令。
- 手动 Artifact 名称不含版本，Tag 名称包含完整 Tag。
- 手动保留 14 天，Tag 中间 Artifact 保留 1 天。
- B 手动模式无 Registry 登录或推送，Tag 模式才需要 `packages: write`。
- B tar 能执行 build、内容检查、健康启动、save/load round-trip。
- 任一基础 workflow 失败都会通过 `needs` 阻止最终 Release。
- `ci.yml` 不再引用 `TERMFLOW_IMAGE`。

实现后的本地验证包括 workflow YAML 解析、release/deploy contract tests、版本检查测试、shell syntax、
B 镜像构建与 tar round-trip。推送后还需要分别手动运行 A、B 和单平台 C 作为远端 Actions 验收；
只有实际 Tag run 才能证明多架构 GHCR 和 GitHub Release 的最终联动。
