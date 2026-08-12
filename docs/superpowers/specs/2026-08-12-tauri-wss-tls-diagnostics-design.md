# Tauri C WSS TLS 与终端诊断设计

## 背景与结论

Windows Tauri C 的普通 API 请求使用启用了 Rustls 的 `reqwest`，而终端通道使用未启用 TLS feature 的 `tokio-tungstenite`。因此本机 `ws://` 可以建立连接，生产环境要求的 `wss://` 无法建立 TLS 连接。共享终端会话把连接异常归一为 1006 并执行指数退避重连，所以界面持续显示“正在恢复”。现有日志只覆盖 HTTP 和授权流程，未覆盖原生终端 WebSocket，无法从现场日志区分 TLS、握手鉴权和服务端关闭。

## 目标

1. 让桌面 Tauri C 可以通过公网 CA 签发的 HTTPS/WSS 服务连接终端。
2. 在不泄露凭据、URL query 或终端内容的前提下，记录终端连接生命周期和稳定错误码。
3. 保持 Rust 侧持有访问令牌、DPoP 私钥和 WebSocket 的既有安全边界。

## 非目标

- 不修改首次启动的默认服务器地址或已保存 issuer 行为。
- 不修改 B 的 WebSocket 协议、认证、重连宽限期或 nginx 配置。
- 不把终端 WebSocket 移回 WebView，不扩大 WebView 网络权限。
- 不在日志中保存 access/refresh token、DPoP proof、完整 URL、query、终端输入或输出。

## TLS 方案

`tokio-tungstenite` 显式启用 `rustls-tls-webpki-roots`。这与现有 `reqwest` 的 Rustls 路线一致，并为 Windows、macOS 和 Linux 的公开 CA 证书提供一致的根证书集合。继续使用现有 `connect_async`、同源目标校验、Authorization 和 DPoP header 生成逻辑。

本次不选择 `native-tls`，避免引入 OpenSSL/Schannel 平台差异；也不选择 `rustls-tls-native-roots`，因为当前生产域名使用公网证书，并不需要企业私有 CA。如果以后正式支持私有 CA，应单独设计信任配置，不能通过关闭证书校验实现。

## 诊断事件与数据边界

Rust 原生终端通道通过现有 `NativeLogger` 直接记录生命周期，避免把底层错误文本交给 WebView。至少记录：

- `terminal_connect_started`：开始连接，仅记录规范化 issuer。
- `terminal_connect_succeeded`：TLS、Upgrade 和鉴权握手完成。
- `terminal_connect_failed`：握手前或握手期间失败，记录稳定 `error_code`。
- `terminal_socket_closed`：连接关闭，记录 WebSocket close code；异常无 close frame 时记录稳定的异常关闭码。

允许的稳定错误码至少区分目标/请求构造失败、TLS/传输连接失败、HTTP 握手拒绝和读流失败。原始依赖错误只用于映射类别，不直接落盘。issuer 继续由 logger 规整为 origin；不得记录 `proof_url`、`socket_url`、Authorization、DPoP、terminal ID、stream ID、query 或帧内容。

共享 `TerminalSession` 的重连语义保持不变：4401/4403 停止重连，其余异常继续按既有退避策略恢复。本次只提高传输能力和可观测性，不改变产品行为。

## 错误流

1. TypeScript 构造同源 HTTPS proof URL 与对应 WSS socket URL。
2. Rust 校验 issuer、路径与 origin，并生成绑定目标的 DPoP header。
3. Rustls 完成 TLS，`tokio-tungstenite` 完成 WebSocket Upgrade。
4. 成功时向前端发送 open/frames；失败时返回现有安全错误码，同时写一条脱敏诊断事件。
5. 前端核心收到失败后沿用既有重连状态机。

## 测试与验收

### 静态与自动化验证

- 依赖合同测试必须证明 `tokio-tungstenite` 启用了 `rustls-tls-webpki-roots`，防止以后回退为仅支持 `ws://`。
- Rust 单元测试覆盖底层错误到稳定错误码的映射、close code 记录和日志脱敏边界。
- 现有 TypeScript 终端传输与重连测试继续通过。
- 运行 Tauri Rust 测试、Tauri workspace 测试、锁文件检查和 `git diff --check`。

### Windows 运行验收

从修复提交重新构建 Windows 安装包，安装后连接 `https://termflow.mcocdaa-newapi.xin`：

1. 普通 API 与授权仍成功。
2. 终端通过 `wss://termflow.mcocdaa-newapi.xin/...` 到达 connected 并收到 `terminal.ready`。
3. `%LOCALAPPDATA%\\io.termflow.client\\logs\\termflow-client.log` 出现 started/succeeded/closed 生命周期事件。
4. 日志中不存在 Token、DPoP、完整 URL query 或终端正文。

Linux 本地 Rust 测试和交叉编译只能证明源码与构建合同，不能代替上述 Windows 安装包和生产 WSS 运行验收。

## 发布与回滚

修复只需要发布新的 Windows C；B、Web C 和 nginx 不需要随此次修复变更。若新包出现回归，可回滚到旧 Windows 包，但旧包仍不支持生产 WSS，不能视为功能性恢复。
