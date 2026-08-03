# TermFlow Tag 派生构建版本设计

日期：2026-08-03

## 背景

当前发布流程把仓库内多个 `0.1.0` 当作产品版本权威。Tag Release 虽然把
`github.ref_name` 传给 A、B + Web C 和原生 C 的基础打包 workflow，但
`scripts/release/check_version.py` 要求 Tag 必须与 Python、npm、Cargo 和 Tauri
清单中的版本完全相同。因此发布 `v0.2.0` 前仍需人工同步修改多个文件；下载后的
A 安装器虽由模板渲染，却仍间接依赖这些手工版本。

本设计将正式发布版本的唯一来源改为 Git Tag。版本在构建 runner 的临时 checkout
中注入，不产生版本修改提交，也不要求发布者在打 Tag 前同步修改多份清单。

## 目标

- Tag Release 以完整的 `v` 前缀 Tag 作为正式构建版本的唯一来源。
- A 的 `termflow --version`、注册时上报的客户端版本、bundle 内 `VERSION` 和安装目录一致。
- B、Web C 和原生 C 的包元数据使用同一构建版本。
- 无 Tag 构建允许通过 `TERMFLOW_BUILD_VERSION` 注入版本。
- 无 Tag 且未注入版本时使用固定默认值 `0.0.0-dev.0`。
- 三个基础打包 workflow 复用同一个版本解析和注入实现。
- Tag、环境变量和默认值均经过格式校验，非法版本在安装依赖或构建产品前失败。

## 非目标

- 不让已安装程序在运行时调用 Git 或读取 `.git`。
- 不自动创建、推送或删除 Git Tag。
- 不在构建后把注入的版本提交回仓库。
- 不改变 Artifact 命名、保留期、平台范围、签名边界或 GHCR 发布权限。
- 不把非 Tag 的手动构建发布成 GitHub Release 或 GHCR 正式版本。

## 备选方案

### 方案 A：构建时统一注入（采用）

统一脚本解析 Tag、环境变量和默认值，再在临时 checkout 中物化所有产品版本。
优点是 Tag 真正成为正式发布权威，所有生态的最终包仍携带正确元数据，且本地和
GitHub Actions 共用同一逻辑。代价是每个独立 checkout 都必须先运行一次版本准备步骤。

### 方案 B：程序运行时读取 Git

Python 可以调用 `git describe`，但已安装的 A、Docker 镜像、Windows 安装包和移动端包
通常没有 `.git`。这种方案会让同一二进制在开发目录和安装目录报告不同版本，因此不采用。

### 方案 C：发布前自动提交版本文件

机器人可批量更新清单、提交并创建 Tag，但此时版本提交仍是权威，Tag 只是复制结果；失败时
还会留下中间提交或需要回滚。它没有消除人工发布状态，只是转移了状态，因此不采用。

## 版本解析规则

统一解析器按以下优先级选择构建版本：

`Git Tag > TERMFLOW_BUILD_VERSION > 0.0.0-dev.0`

1. 显式传入的 Git Tag；
2. `TERMFLOW_BUILD_VERSION` 环境变量；
3. 固定默认值 `0.0.0-dev.0`。

Tag 必须为 `v` 前缀、且去掉 `v` 后是 TermFlow 支持的 SemVer，例如 `v0.2.0` 或
`v0.2.0-rc.1`。环境变量不带 `v`，例如：

```bash
TERMFLOW_BUILD_VERSION=0.2.0 <build command>
```

如果同时存在 Tag 和环境变量，Tag 无条件优先。这样仓库或 runner 中残留的环境变量不能改变
Tag Release 的包版本。空字符串等同于未设置；非法的非空值必须失败，不能静默回退默认值。

解析器同时输出：

- `release_tag`：Tag 模式保留原始 Tag；非 Tag 模式为 `v${version}`，仅供 A 安装器和内部目录使用；
- `version`：不含 `v` 的产品构建版本；
- `is_release`：只有显式 Tag 模式为真。

`0.0.0-dev.0` 只是非正式构建的安全默认值。它不能触发 GHCR 推送、GitHub Release、
`latest` 更新或正式 Artifact 命名。

## 版本物化边界

新增一个结构化版本物化脚本。它只修改当前 checkout，并在构建开始前完成以下工作：

- Python：Node、Control Plane 和 Protocol 的 `pyproject.toml` 版本，以及 A 的运行时
  `__version__`；Control Plane 对外报告版本时读取包版本，不再写死 `0.1.0`。
- npm：根 workspace、Web C、Tauri C 和共享客户端包的 `package.json` 版本及内部依赖版本。
- Tauri/Rust：`Cargo.toml` 和 `tauri.conf.json` 的应用版本。
- 锁文件：只更新 workspace 自身的版本和内部依赖引用；第三方包名称、版本、校验和与来源保持不变。

脚本完成后再次读取所有受管表面，验证它们与解析出的构建版本一致。它必须使用 JSON/TOML
结构定位或受约束的包名定位，不能全局替换所有 `0.1.0`，避免修改测试样例、第三方锁文件条目
或业务数据。

仓库中受管表面的静态版本改为开发占位值 `0.0.0-dev.0`。该值只服务于未执行版本准备步骤的
本地开发和编辑器，不代表任何正式 Release。

## Workflow 数据流

### Tag Release

`.github/workflows/release.yml` 在编排开始时只验证 Tag 格式并解析版本，不再要求 Tag 与源码中的
开发占位版本相等。它继续把完整 `github.ref_name` 传给三个可复用 workflow。

每个基础 workflow 的每个独立构建 checkout 都执行统一版本准备步骤：

1. checkout 对应 Tag 的提交；
2. 以 `release_tag` 调用解析器；
3. 物化各生态版本；
4. 验证物化结果；
5. 再执行 `uv sync --frozen`、`npm ci`、Cargo/Tauri 或 Docker build。

Tag 模式的 Artifact 名称、GitHub Release 名称和 GHCR 标签继续使用完整原始 Tag。A 安装器中的
固定 `TAG` 由同一个解析结果渲染，因此 `termflow --version`、bundle `VERSION` 与安装器 Tag
不能分叉。

### 手动基础 Workflow

三个 `workflow_dispatch` 表单增加可选的 `version` 输入。workflow 将其映射到
`TERMFLOW_BUILD_VERSION`；调用者不填写时使用 `0.0.0-dev.0`。可复用 `workflow_call` 也接受
可选的非发布构建版本，方便其他验证 workflow 调用，但它不能使 `is_release` 变为真。

手动 Artifact 的外层名称继续保持：

- `termflow-node-linux-x86_64`
- `termflow-control-plane`
- `termflow-<platform>`

手动 B 不登录或推送 GHCR，手动 C 不上传商店，手动 A 的安装器可继续通过
`TERMFLOW_RELEASE_BASE_URL=file://...` 离线安装。

### 本地构建

本地脚本直接读取同一个环境变量。例如：

```bash
TERMFLOW_BUILD_VERSION=0.2.0 scripts/release/build_node_bundle.sh \
  release-assets
```

若脚本仍接收显式 Tag 参数，该参数属于最高优先级的 Tag 输入；实现不得同时维护第二套版本解析规则。

## 失败处理

- Tag 缺少 `v`、版本格式非法或无法被受支持的 Python/npm/Cargo/Tauri 版本格式共同表示：立即失败。
- `TERMFLOW_BUILD_VERSION` 非空但非法：立即失败，不使用默认值。
- 物化后任一受管文件版本不一致：立即失败并列出文件，不进入产品构建。
- 锁文件物化会改变第三方依赖：测试失败，禁止发布。
- Tag Release 中任一基础 workflow 失败：保持现有依赖关系，不推送后续 GHCR，也不创建 GitHub Release。

## 测试与验收

版本解析单元测试覆盖：

- Tag 优先于环境变量；
- 无 Tag 时使用合法环境变量；
- 两者都没有时得到 `0.0.0-dev.0`；
- 非法 Tag 和非法环境变量失败；
- stable、prerelease 和 build metadata 的受支持版本；
- Tag 与环境变量冲突时不会污染正式版本。

版本物化测试在临时仓库副本中验证：

- 所有受管产品表面更新为目标版本；
- A 的 `--version` 来源、注册上报版本和 bundle `VERSION` 一致；
- npm 内部依赖与 workspace 包版本一致；
- uv、npm 和 Cargo 锁文件只改变 TermFlow 自身条目；
- 测试夹具中的示例 `0.1.0` 不被误改；
- 第二次执行结果不再变化，保证幂等。

Workflow 契约测试验证：

- Release 将完整 Tag 传入三个基础 workflow；
- 每个实际构建 checkout 都在安装依赖前物化版本；
- 手动 workflow 的可选版本被映射为 `TERMFLOW_BUILD_VERSION`；
- 无输入时使用固定开发默认值；
- 只有显式 Tag 能进入 GHCR 和 GitHub Release 路径；
- 既有 Artifact 名称、保留期和权限边界不变。

最终本地验收至少包括版本测试、release/workflow 契约测试、A bundle 构建与 `--version` 检查、
Python `--frozen` 同步检查、npm `ci`/测试/类型检查、Rust 编译以及 shell/YAML 静态检查。真实 Tag
发布仍需远端 Actions 运行证明；本地测试不冒充 GHCR 或 GitHub Release 的线上发布证据。
