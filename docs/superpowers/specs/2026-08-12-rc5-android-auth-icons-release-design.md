# TermFlow v0.1.0-rc.5 Android Auth, Icons, and Release Design

**日期：** 2026-08-12  
**状态：** 用户已确认设计  
**范围：** Android C、Tauri C、B Control Plane、Web C、RC 发布流水线

## 1. 目标

rc.5 同时解决三类已观察问题：

1. Android“其他设备授权”点击后错误访问旧的 `127.0.0.1` 地址；
2. Android 本机浏览器 OAuth 路径尚未完成真实登录验收；
3. Android 发布 APK 显示 Tauri 默认的黄色/蓝色斜“8”，而不是 TermFlow `>_` 图标。

rc.5 还建立 Android 后续升级基线：旧 rc.3/rc.4 安装包需要卸载一次，rc.5 起使用固定签名；之后更高版本使用同一签名和递增版本号直接覆盖安装。

## 2. 已确认的根因和边界

### 2.1 跨设备授权地址丢失

`NativeConnectView.vue` 的“其他设备授权”按钮直接跳转 `/connect/device`，没有执行与本机浏览器登录相同的 `canonicalIssuer` 和 `serverConfig.replace`。设备页启动时读取 `serverConfig.current`，因此用户输入私有域名后仍可能用默认 `http://127.0.0.1:8765` 拉取 metadata，最终显示网络错误。

修复边界是抽出共享的服务器准备流程，两个入口都必须先规范化、持久化、拉取并校验 OAuth metadata。设备页仍需独立复核 issuer，不能信任路由 query 作为服务器地址。

### 2.2 Android 图标未进入 APK

仓库存在 TermFlow 图标素材，但 rc.3/rc.4 实际 APK 的 `ic_launcher`、`ic_launcher_round` 和 `ic_launcher_foreground` 是 Android/Tauri 模板资源。原因是 `tauri android init` 生成独立 Gradle 工程后，发布流水线没有确定性地把仓库素材同步到生成工程再构建。

桌面 Tauri 配置的 `bundle.icon` 只列出桌面图标路径；Android 图标属于生成工程的 `app/src/main/res` 资源，不能由 Windows 包正常显示来证明 Android 包正确。

### 2.3 覆盖安装基线

本机 rc.3 与 rc.4 APK 都是 `CN=Android Debug`，但证书 SHA-256 指纹不同，因此不同批次 debug APK 不能互相覆盖。当前开发配置的 Android `versionCode` 也不能作为正式升级策略。

Android 更新验收要求：相同 application ID、相同签名证书、更新包的 versionCode 不低于已安装包。rc.5 接受一次卸载迁移，从 rc.5 起固定 keystore 并保证 versionCode 单调递增。

## 3. 设计

### 3.1 共享服务器准备流程

新增一个 Tauri 前端可测试的共享准备函数，输入用户填写的 issuer，输出 canonical issuer 和 OAuth metadata。函数执行：

```text
用户输入 issuer
  -> canonicalIssuer
  -> serverConfig.replace
  -> GET /.well-known/oauth-authorization-server
  -> metadata.issuer 必须等于 canonical issuer
```

本机浏览器入口继续把 metadata 的 authorization endpoint、scopes 和 canonical issuer 交给现有 `NativeAuthorizationSession`。跨设备入口使用同一准备结果创建设备码。设备页进入时不从 query 接受任意服务器地址，只使用持久化的已校验 issuer。

错误必须保持可操作且不暴露凭据：地址非法、远程 HTTP、网络离线、issuer 不匹配、能力拒绝、设备码过期、审批拒绝和回调失败分别映射到现有稳定错误码/中文提示。设备码、PKCE verifier、DPoP 私钥、token 和完整 callback URL 不写日志。

### 3.2 本机浏览器与跨设备登录验收

两条路径最终都进入同一个 native credential vault 和受保护 API 验证：

```text
本机浏览器：PKCE/state -> 系统浏览器 -> Web C 审批 -> termflow:// 回调 -> token exchange
跨设备：device code -> 另一会话 Web C 审批 -> 轮询 token endpoint -> token exchange
                                      \-> verify protected API -> workspace
```

本机浏览器必须在 Android 真机验证系统浏览器打开、Web C 登录/审批、deep-link 回调、PKCE/DPoP 换 token、受保护 API 和重启后的凭据恢复。跨设备必须验证私有域名被保存并实际用于 metadata/device-code/token 请求。

### 3.3 Android 图标确定性同步

以 `apps/clients/tauri/app-icon.svg` 为设计源。Android 初始化后执行 Tauri icon 生成或等价的确定性同步，确保以下生成工程资源均来自 TermFlow 素材，而不是模板：

- `mipmap-*/ic_launcher.png`
- `mipmap-*/ic_launcher_round.png`
- `mipmap-*/ic_launcher_foreground.png`
- adaptive icon 的 foreground/background 引用

Android 前景和背景按 adaptive icon 分层生成，前景不再携带错误模板资源。构建前检查生成工程资源；构建后从 APK 解包检查 launcher 文件及其内容/哈希，拒绝发现默认 Tauri 图标特征的包。真实 Android 桌面显示是最终验收，不以 Linux 源码检查替代。

### 3.4 rc.5 签名与版本

rc.5 使用 CI 注入的固定 Android keystore：keystore 文件和密码只来自 GitHub Secrets，生成 `gen/android/keystore.properties`，构建结束后清理临时文件。仓库不提交私钥、properties 或签名产物。

版本物化器为 rc.5 生成高于 rc.4 的 Android versionCode，并对 rc prerelease 保持单调规则；计划中将补充明确的 rc 序号映射和测试，避免所有 `0.1.0-rc.N` 产生相同 versionCode。APK 静态门禁检查 application ID、versionName、versionCode、证书指纹和图标资源。

### 3.5 测试矩阵

- Client core/Tauri 单测：共享服务器准备、入口跳转、设备授权状态机、错误映射和日志脱敏。
- B/Web C E2E：设备码创建/审批/轮询成功、pending/slow_down/deny/expire；同设备 browser OAuth/PKCE/deep-link 回归。
- Android artifact：`io.termflow.client`、rc.5 版本、固定证书、TermFlow launcher 资源、无默认斜“8”。
- Android 真机：卸载 rc.4 后安装 rc.5，桌面图标、跨设备授权、本机浏览器授权、重启恢复。
- 升级：同一 rc.5 keystore 构建更高 versionCode，直接覆盖且数据保留。
- Windows 回归：现有本机浏览器 OAuth 和 WSS 终端连接保持正常。

## 4. 非目标

- 不在 Tauri 中收集或持久化管理员 Token；
- 不把 Android 的修复降级为 HTTP/WS；
- 不把旧 rc.3/rc.4 的临时 debug 证书伪装成可迁移签名；
- 不以 Windows/Linux 本地构建通过宣称 Android 真机或生产部署已通过；
- 不在本次引入 Play 商店/AAB 发布体系。
