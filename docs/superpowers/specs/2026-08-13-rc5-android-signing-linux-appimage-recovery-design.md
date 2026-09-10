# TermFlow rc.5 Android Signing and Linux AppImage Recovery Design

**日期：** 2026-08-13  
**状态：** 用户已确认设计  
**范围：** Android 固定发布签名、GitHub Actions secrets、Linux deb/AppImage 打包可靠性

## 1. 目标与边界

本次恢复 `v0.1.0-rc.5` 发布流水线中的两个独立失败：

1. Android tag 构建因五个签名 secret 均为空而在签名前失败；
2. Linux 已成功生成 deb，但 AppImage 在 Tauri 调用 `linuxdeploy` 时失败。

本次不接入 Windows Authenticode/SignPath、macOS Developer ID/notarization、iOS 真机/TestFlight、
Linux GPG 或应用商店发布。Windows 保持未签名 NSIS，macOS 保持 ad-hoc 签名，iOS 保持
Simulator-only；现有 GHCR 容器镜像继续使用 cosign keyless 签名。

## 2. 已确认事实

- Release run `31618251120` 中 Windows、macOS、iOS Simulator、Node package、Node image publish
  成功；Linux 和 Android 失败，后续 Control Plane 与 GitHub Release 被依赖关系跳过。
- Android workflow 已正确声明、继承和逐项检查
  `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、
  `ANDROID_KEY_PASSWORD`、`ANDROID_SIGNING_CERT_SHA256`；运行日志显示它们均为空。
- Linux 同一次构建已完成 Rust release 和 deb，仅 AppImage bundling 失败。Tauri 2.11.4 的
  AppImage bundler 在运行时下载 AppRun、linuxdeploy、GTK/GStreamer plugin 和 AppImage output
  plugin；本机诊断也观察到 AppRun 下载 `timeout: global`，说明外部工具下载是独立的可靠性边界。
- GitHub CLI 当前保存的 token 无效；设备登录请求曾因访问 GitHub device endpoint 超时而失败。
  写入 secrets 前必须重新确认 CLI 登录和仓库管理员权限。

## 3. Android 固定签名材料

### 3.1 生成

在当前受控主机的临时目录中生成一份长期固定 JKS：

- RSA 4096；
- 显式使用传统 `JKS` storetype，使 store password 与 key password 可保持为两个独立值；
- 有效期 10000 天；
- alias、store password 和 key password 由密码学安全随机源分别生成；
- subject 只包含稳定的 TermFlow 项目标识，不包含个人地址或电话；
- 从 keystore 导出并规范化唯一 signer 证书的 SHA-256 指纹。

生成后必须先用 `keytool -list` 校验 alias、算法、有效期和指纹，再形成 GitHub secrets。不得把
JKS、密码、base64、私钥或明文恢复清单写入仓库、日志、命令行参数或聊天。`keytool` 通过
`:env` 密码源读取临时环境变量，环境变量只存在于生成命令的进程环境中。

### 3.2 恢复包

恢复包包含：

- 固定 JKS；
- alias；
- store password；
- key password；
- signer SHA-256；
- 生成日期、用途、恢复与指纹校验命令。

恢复包使用现有 `/home/mcocdaa/.ssh/id_github.com.pub`（Ed25519）作为 `age` SSH recipient 加密，
输出到仓库外：

```text
/home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age
```

必须先验证该公钥指纹为
`SHA256:Ct/eCz69lu4sPwIh4DHTSFisVd8dV8vuxw5kfUbjsqg`，并用对应私钥完成一次解密恢复演练。
恢复演练成功、密文文件权限收紧为仅当前用户可读写之后，才删除临时明文目录。密文必须由用户
另行复制到独立备份位置；本次工作不能把“主机上有一个密文文件”宣称为双备份。

若主机缺少 `age`，只安装发行版提供的 `age` 包；不实现自定义加密格式，也不把秘密退化为普通
tar、仅文件权限保护或仓库内加密文件。

### 3.3 GitHub Secrets

重新完成 `gh auth login` 后先验证：

- 当前账号是目标维护者账号；
- `mcocdaa/TermFlow` 的 `viewerPermission` 为 `ADMIN`；
- 远端仓库是预期公开仓库。

然后通过 stdin 写入五个 repository Actions secrets，禁止值出现在 shell 参数和输出中。上传完成后
只列出 secret 名称和更新时间，不读取或打印值。`ANDROID_KEYSTORE_BASE64` 使用无换行 base64；
`ANDROID_SIGNING_CERT_SHA256` 使用现有 verifier 接受的规范化十六进制指纹。

## 4. Linux deb/AppImage 恢复

### 4.1 构建隔离

Linux job 保持同一个 Ubuntu 22.04 runner，但拆成两个 Tauri bundle 命令：

1. `--bundles deb --ci`；
2. `--bundles appimage --ci --verbose`。

这样日志能准确区分产品编译、deb 打包和 AppImage/linuxdeploy；deb 的成功产物不会被误判为从未
生成。正式 job 仍要求两种介质都存在，缺一则整个 native-client reusable workflow 失败。

### 4.2 有界重试与诊断

AppImage 命令最多执行三次。每次失败后：

- 保留原始退出码；
- 输出当前 Tauri tool cache 中工具的文件名、大小和 SHA-256，不输出环境或 secrets；
- 保留 `--verbose` 下的 linuxdeploy stderr；
- 仅删除不完整的 AppImage 输出目录，不删除 Cargo target、deb 或已完整下载的工具缓存；
- 等待固定 10 秒后重试。

第三次仍失败时返回最后一次真实退出码。重试只吸收 GitHub/raw.githubusercontent.com 下载或
一次性 linuxdeploy 失败，不把真实的依赖缺失、ABI 或插件错误隐藏为成功。

### 4.3 当前不采用固定镜像

本次不把 Tauri 下载的 AppRun/linuxdeploy/plugin 二进制提交进仓库，也不自建 release-tool 镜像。
原因是当前目标是恢复 rc.5，且 upstream URL 中包含 mutable `master`/`continuous` 资源；未经完整
版本选择、校验和维护策略，不应假装已经完成供应链固定。workflow 的详细诊断将为后续固定工具链
提供所需版本和 hash 证据。

## 5. 测试与验收

### 5.1 静态测试

先扩展 release workflow contract tests，使其在实现前失败，并要求：

- deb 与 AppImage 为两个独立命令；
- AppImage 命令带 `--verbose`；
- 最多三次、有界等待、最后真实退出码；
- 失败诊断只列工具元数据，不打印环境；
- 最终仍要求且上传恰好一个 deb 和一个 AppImage；
- Android 五个 secret、签名注入、证书校验和清理顺序保持不退化。

实现后运行相关 release tests、完整 `tests/release`、YAML 解析、shell 静态检查和 `git diff --check`。

### 5.2 Android 在线候选验收

Secrets 配置后手动运行 `Package C · Native Clients`：

- `platform=android`；
- `version=0.1.0-rc.5`；
- `signed_android_candidate=true`。

必须等待 job 成功并下载最终 APK，再核对 application ID、versionName/versionCode、唯一 signer
SHA-256、launcher 资源和 APK SHA-256。Actions 成功只证明已签名产物静态合同；Android 真机登录、
重启恢复和未来 `adb install -r` 升级仍是独立运行时验收。

### 5.3 Linux 在线验收

workflow 修改推送后单独运行 `platform=linux`，必须观察：

- deb 命令成功；
- AppImage 命令成功；如果发生重试，日志包含不泄密的工具元数据和原始错误；
- artifact 同时包含一个 `.deb` 和一个 `.AppImage`。

本地主机是 Ubuntu 24.04/WSL，不能替代 GitHub Ubuntu 22.04 兼容性产物证明。

## 6. rc.5 恢复策略

不移动、不删除、不覆盖已经推送的 `v0.1.0-rc.5` tag。若仅配置 Android secrets，允许对原 run
执行 GitHub Actions rerun，因为其 workflow 源仍是 rc.5 tag。若 Linux 修复需要提交 workflow
改动，则该改动不可能改变既有 tag 的 workflow 内容；应先通过手动 packaging workflow 验证，并在
用户明确要求发布时创建递增的新 prerelease tag，而不是伪造 rc.5 已被源码修复。

本次设计和后续实现提交本身不授权创建 tag、移动 tag、发布 Release 或推送远端；这些交付动作需要
用户另行明确要求。

## 7. 完成条件

- 加密恢复包已完成真实解密演练，临时明文已清除；
- 五个 Android repository secrets 已配置且名称/更新时间可见；
- signed Android candidate 的静态签名合同通过；
- Linux workflow 静态合同通过，远端 Linux packaging 同时产生 deb/AppImage；
- 未泄露任何签名材料；
- 未改变 Windows/macOS/iOS 的既定签名边界；
- 未把静态测试、workflow 触发或进行中状态表述为真实发布成功。
