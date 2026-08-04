# TermFlow 日志规范设计

## 目标

为 A（Linux Node/Bridge）、B（Control Plane）、Web C 和 Tauri C 建立明确、可诊断且不泄露凭据的日志边界。日志用于定位安装、启动、网络、认证和授权问题，不保存终端正文或认证秘密。

## 组件边界与路径

### A：Node CLI 与 Bridge

A 使用 `platformdirs.user_log_path("termflow")` 作为日志根目录，让不同操作系统遵循系统目录约定：

- Linux：`~/.local/state/termflow/log/`
- macOS：`~/Library/Logs/termflow/`
- Windows：`%LOCALAPPDATA%\\termflow\\Logs\\`

主进程与 CLI 事件写入 `termflow.log`。每个 Bridge 允许写入同一目录下按 Instance 标识命名的日志文件；现有 Instance 目录中的 `bridge.log` 不再作为唯一诊断入口，但保留兼容读取能力。

### B：Control Plane

B 不新增应用文件日志。容器继续将应用和 Uvicorn 日志输出到 stdout/stderr，由 Docker logging driver 管理；运维通过 `docker compose logs` 或部署平台的容器日志系统查看。B 现有 `X-Request-ID` 响应头作为 Web C 与服务端日志的关联 ID。

### Web C

Web C 不写浏览器磁盘，不使用 localStorage、IndexedDB 或 sessionStorage 保存日志。前端只允许输出脱敏的开发者 console 事件；正式诊断依赖 B 的请求 ID、HTTP 状态和错误 code。终端内容、Cookie、Token 和表单秘密不得进入 console。

### Tauri C：桌面与移动客户端

Tauri C 使用 Tauri 的应用日志目录解析器：

- Windows：`%LOCALAPPDATA%\\io.termflow.client\\logs\\`
- Linux：Tauri 对应的用户应用日志目录
- macOS：Tauri 对应的用户应用日志目录
- Android/iOS：使用平台应用沙盒中的日志目录

文件名为 `termflow-client.log`，桌面与移动端共享事件格式。日志目录不与安装目录、当前工作目录或临时目录绑定。

## 格式与轮转

每行使用 JSON Lines，字段至少包含：

```json
{"timestamp":"2026-08-04T00:00:00Z","component":"tauri","level":"error","event":"token_exchange_failed","issuer":"http://127.0.0.1:8765","request_id":"...","error_code":"authorization_required"}
```

`timestamp` 使用 UTC RFC 3339；`component`、`level`、`event` 和 `error_code` 使用固定枚举或小写标识。默认单文件 10 MiB，保留 5 个历史文件。轮转失败不能阻塞业务流程。

## 授权诊断事件

Tauri C 至少记录以下阶段：

- `connect_started`
- `metadata_success`
- `metadata_failed`
- `browser_open_started`
- `browser_open_failed`
- `authorization_callback_received`
- `authorization_callback_invalid`
- `token_exchange_succeeded`
- `token_exchange_failed`

日志只记录服务器 origin、请求 ID、状态和错误 code，不记录完整授权 URL、query、PKCE、DPoP 或公钥材料。

## 隐私与安全

任何组件都不得写入：管理员 Token、注册码、Installation/Instance Credential、Cookie、TOTP 秘密或验证码、PKCE verifier/challenge、DPoP 私钥/Token、终端输入、终端输出和完整 URL query。异常对象只允许转换成稳定的错误 code；原始错误文本需要经过脱敏或丢弃。

## 验证

- A：测试系统日志目录解析、JSON 字段、轮转和秘密脱敏；验证 CLI 与 Bridge 的日志入口。
- Tauri：测试日志目录使用应用日志目录、授权阶段事件和秘密脱敏；桌面/移动配置都必须保留日志权限。
- Web C/B：保留现有 `X-Request-ID` 合同，验证前端不会把凭据或终端内容写入持久化存储或日志。
- 文档：README 和故障排查文档列出各平台日志路径以及 Docker 日志命令。
