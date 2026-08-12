# Android 发布与升级手册

本手册适用于 TermFlow `v0.1.0-rc.5` 及后续 Android APK。`rc.3`、`rc.4`
使用过不同的 debug 证书，不能直接覆盖安装；`rc.5` 是固定项目签名的升级基线。

## 1. 建立并保管固定签名

在受控的离线环境中生成项目 keystore。alias、密码和路径由发布负责人确定，不要把示例值、
真实密码或 keystore 放入仓库、issue、Actions 日志或验收记录。

```bash
keytool -genkeypair -v \
  -keystore termflow-android-release.jks \
  -alias <release-alias> \
  -keyalg RSA -keysize 4096 -validity 10000

keytool -list -v \
  -keystore termflow-android-release.jks \
  -alias <release-alias>
```

记录证书的 SHA-256 指纹，并保存至少两份相互独立的加密备份。备份要包含 keystore、alias、
store password、key password、证书指纹和恢复演练记录。明文材料不得保存在普通云盘或开发机。

证书或 keystore 丢失后，不得生成另一份同包名证书并宣称可以覆盖升级。此时必须停止 Android
发布，设计新的 application ID 和明确的数据迁移方案。

## 2. 配置 GitHub Actions secrets

在仓库 Actions secrets 中配置以下五项；名称必须完全一致：

```text
ANDROID_KEYSTORE_BASE64
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
ANDROID_SIGNING_CERT_SHA256
```

`ANDROID_KEYSTORE_BASE64` 是固定 keystore 的单行 base64。`ANDROID_SIGNING_CERT_SHA256` 必须是
该 keystore 唯一 signer 证书的 SHA-256，不是 Android debug key 的指纹。配置后由另一名发布者
核对 secret 名称和证书指纹；不要在命令行历史或日志中回显 secret 值。

## 3. tag 前构建候选包

在非 tag 分支打开 `Package C · Native Clients`：

1. 选择 `platform=android`、`version=0.1.0-rc.5`、
   `signed_android_candidate=false`，验证开发用 debug 路径仍能构建。
2. 再选择 `platform=android`、`version=0.1.0-rc.5`、
   `signed_android_candidate=true`，生成与 tag release 相同签名方式的候选 APK。
3. 两次手动运行都只生成短期 artifact，不创建 tag 或 GitHub Release。

signed candidate 缺少任一 signing secret 时必须失败。上传前 workflow 会检查：

- package 为 `io.termflow.client`；
- `versionName=0.1.0-rc.5`、`versionCode=10065`；
- signer 证书等于 `ANDROID_SIGNING_CERT_SHA256`；
- 普通、round、adaptive launcher 均来自 TermFlow `>_` 素材，而不是 Tauri 斜“8”模板。

下载 `TermFlow-0.1.0-rc.5-android-arm64.apk` 后记录 Actions run URL 和 APK SHA-256。
自动检查只证明产物静态合同；它不能代替 Android 真机或 Windows 实机验收。

## 4. rc.3/rc.4 到 rc.5 的一次性迁移

在真机执行并记录每一步：

1. 从 rc.3/rc.4 导出需要保留的非秘密信息。
2. 卸载旧 TermFlow；旧 debug 签名与 rc.5 固定签名不同，不能覆盖安装。
3. 安装 rc.5 release APK，并重新登录一次。
4. 确认桌面和应用列表显示 TermFlow `>_` 图标。
5. “在其他设备上授权登录”：确认私有 HTTPS 域名用于 metadata、device code 和 token 请求，
   另一台已登录 Web C 能审批并让手机完成登录。
6. “在本机浏览器授权登录”：确认系统浏览器打开、Web C 登录/审批、
   `termflow://auth/callback` 回调、token 换取和受保护 API 全部成功。
7. 强制停止并重启 App，确认服务器配置和凭据恢复。

日志和验收记录必须移除 `device_code`、`user_code`、token、cookie、密码和私钥。

## 5. 验证 rc.5 之后可覆盖升级

在测试设备或可恢复快照上，用同一份固定 keystore 构建未发布的
`0.1.0-rc.6` signed candidate；其 `versionCode` 应为 `10066`。先保留已登录的 rc.5，再执行：

```bash
adb install -r TermFlow-0.1.0-rc.6-android-arm64.apk
```

必须确认无需卸载、应用数据和凭据仍存在、受保护 API 可用。证书、application ID 或
versionCode 任一不符合时都不得发布。测试完成后不要把此候选误标为正式 `rc.6`。

## 6. tag 发布门禁

只有候选 APK、Android 真机和 Windows 实机均完成后，才创建 `v0.1.0-rc.5` tag。
tag workflow 必须在 Android job 中完成 release signing 和 package/version/cert/icon 检查，
并等待全部 A、B + Web C、Windows/Linux/macOS/Android/iOS Simulator job、publish、provenance
及镜像验证成功。workflow 已触发或仍在运行不等于发布成功。

Windows 回归至少覆盖安装/升级、私有域名登录、终端连接稳定性和日志；Windows 成功不能替代
Android 结果，Android 成功也不能替代 Windows 结果。

## 7. 验收证据

不得用计划值填充“实际结果”。每次发布复制此表并填写证据：

| 项目 | 实际证据 |
| --- | --- |
| commit / tag | |
| Actions candidate / release run URL | |
| APK 文件名 / SHA-256 | |
| package | |
| versionName / versionCode | |
| signer cert SHA-256 | |
| launcher 静态检查 | |
| 真机型号 / Android 版本 | |
| 其他设备授权结果 | |
| 本机浏览器授权结果 | |
| 重启恢复结果 | |
| `adb install -r` 覆盖升级结果 | |
| Windows 版本 / 安装包 SHA-256 | |
| Windows 登录 / 终端稳定性结果 | |
| 已脱敏日志位置 | |
