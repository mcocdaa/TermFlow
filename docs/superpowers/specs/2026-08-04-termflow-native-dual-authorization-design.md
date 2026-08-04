# TermFlow 原生客户端双路径授权设计

**日期：** 2026-08-04  
**状态：** 待用户审阅  
**范围：** Tauri C、B Control Plane、Web C 授权页面和共享 OAuth 契约

## 1. 背景与目标

当前 Tauri C 使用 OAuth authorization-code + PKCE：客户端打开本机系统浏览器，Web C 审批后通过
`termflow://` 回调回到客户端。这个流程适用于管理员和 Tauri 在同一台设备，但不能覆盖“客户端在
Computer A、管理员在手机或另一台电脑”的场景。

本设计保留当前本机浏览器流程，同时增加跨设备设备码流程。两条路径都由 B 维护授权事务，最终
换取相同的 native access/refresh credential；管理员 Token 永远只在 Web C 输入，Tauri 不接收或
持久化管理员 Token。

设计参考 OAuth 2.0 Device Authorization Grant（RFC 8628）和 GitHub 同时提供 Web OAuth 与
Device Flow 的做法：

- https://datatracker.ietf.org/doc/html/rfc8628
- https://docs.github.com/en/enterprise-cloud@latest/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps

## 2. 用户体验

Tauri“连接到服务器”页面保留服务器地址输入，并提供两个授权入口：

1. **在本机浏览器授权**：默认入口。客户端打开系统浏览器，用户在同一设备的 Web C 完成审批，
   通过 `termflow://auth/callback` 回到客户端。
2. **在其他设备上授权**：跨设备入口。客户端显示一次性设备码、有效期、轮询状态和二维码；
   管理员在手机或另一台电脑打开 Web C 的设备授权页面，输入或扫描设备码并审批。

两条路径都显示明确的状态：等待审批、已拒绝、已过期、审批成功、网络错误。设备码页面提供
“复制设备码”和“重新生成”操作，不显示管理员 Token 输入框。

## 3. 共享授权请求模型

两条路径共享以下原生客户端声明：

- `client_name`
- `platform`
- `client_version`
- `scopes`
- DPoP 公钥/JWK thumbprint
- 一次性 PKCE `code_challenge`（本机浏览器流程必需；设备流程也使用，用于绑定客户端轮询）

B 为每次授权创建一个短期事务。事务包含客户端展示信息、状态、过期时间、拒绝失败计数、授权
码状态和设备流程状态。现有 Web C 审批接口继续操作这条事务，不创建第二套授权记录。

## 4. 本机浏览器流程（保留）

现有流程保持不变：

```text
Tauri 生成 state + PKCE + DPoP key
  → GET /api/v1/oauth/authorize?...（系统浏览器）
  → Web C 登录、查看客户端信息、审批或拒绝
  → termflow://auth/callback?state=...&transaction_id=...
  → Tauri POST /api/v1/oauth/token（authorization_code + PKCE）
```

Tauri 只在用户选择“本机浏览器授权”时调用系统浏览器。浏览器打开失败应显示准确的错误原因，
并写入脱敏诊断日志；这不影响设备码入口。

## 5. 跨设备设备码流程

### 5.1 申请设备码

新增公开接口：

```http
POST /api/v1/oauth/device/code
Content-Type: application/json
```

请求复用共享授权请求字段，并包含 `code_challenge`、DPoP JWK 和客户端元数据。B 返回：

```json
{
  "device_code": "long-lived-random-secret",
  "user_code": "ABCD-EFGH",
  "verification_uri": "https://relay.example.com/device",
  "verification_uri_complete": "https://relay.example.com/device?code=ABCD-EFGH",
  "expires_in": 900,
  "interval": 5
}
```

`device_code` 只返回给 Tauri，使用加密随机值并且只在 B 中保存摘要；`user_code` 用于 Web C
输入或二维码快速填充，也只保存摘要。两者都在过期后失效。

### 5.2 Web C 审批

新增 Web C 路由 `/device`：

1. 输入或读取 `user_code`；
2. 展示待授权 Computer、平台、客户端版本和申请时间；
3. 要求管理员 Web 会话；
4. 如果启用了双重因素认证，要求 TOTP；
5. 管理员选择批准或拒绝。

二维码只编码 `verification_uri_complete`，不包含 `device_code`、管理员 Token 或访问凭据。二维码
颜色使用当前主题色。

### 5.3 Tauri 轮询

Tauri 使用 `device_code` 调用现有 token endpoint 的扩展 grant：

```http
POST /api/v1/oauth/token
Content-Type: application/json
```

```json
{
  "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
  "device_code": "...",
  "code_verifier": "..."
}
```

服务端返回以下状态：

- `authorization_pending`：继续等待；
- `slow_down`：增加轮询间隔；
- `access_denied`：停止并提示管理员拒绝；
- `expired_token`：停止并提供重新生成设备码；
- 成功：返回与 authorization-code 流程相同的 native access/refresh credential。

Tauri 必须遵守服务端返回的最小 `interval`，默认 5 秒；禁止高频轮询。设备码只能被成功兑换
一次，兑换成功后立即失效。

## 6. 安全边界

- 管理员 Token 只在 Web C 的登录表单中出现；Tauri、设备码响应、二维码和日志均不包含它。
- `device_code` 是高熵秘密，日志、前端分析和错误消息不得记录；`user_code` 是短期一次性
  关联码，默认有效期 15 分钟。
- B 对申请、设备码兑换、Web C 输入和管理员审批分别限流；错误状态不能泄露设备是否属于
  某个真实 Computer。
- 设备授权页面必须显示客户端名称、平台、版本和申请时间，让管理员确认请求来源。
- 成功后的访问凭据仍使用现有 DPoP 绑定和撤销机制；设备流程不新增长期 Bearer Token。
- 同一设备码最多批准并兑换一台 Computer；并发兑换必须由数据库条件更新保证原子性。

## 7. API 与前端契约变更

- OAuth metadata 增加 `device_authorization_endpoint` 和设备验证页面信息，客户端不硬编码公网
  地址。
- `packages/client-contracts` 增加 device-code response、poll 状态和错误类型。
- `packages/client-core` 增加可注入的 device authorization/polling session；现有
  `NativeAuthorizationSession` 保留。
- B 增加 device-code 创建、查询/兑换和状态转换；现有 `/api/v1/oauth/authorize` Web 审批接口
  保持兼容。
- Web C 增加 `/device` 设备审批页，沿用当前管理员会话、TOTP 和主题二维码组件。
- Tauri 登录页显示两个入口；设备流程不依赖 opener、deep-link 或系统浏览器。

## 8. 错误和生命周期

设备码申请失败、B 不支持设备流程、网络中断和授权过期都必须给出可操作提示。轮询过程中关闭
客户端时，内存中的 `device_code` 和 PKCE verifier 丢弃；B 的事务自然过期。重新申请必须生成
全新的设备码和 PKCE 对，不能复用旧事务。

本机浏览器流程的回调校验、PKCE、DPoP 和现有错误语义保持不变。两种流程成功后进入同一个
`TokenSession`，不在 UI 层复制凭据存储或刷新逻辑。

## 9. 测试与验收

- B：设备码随机性、摘要存储、TTL、限流、状态转换、并发批准/兑换、TOTP、重放和错误响应。
- 契约：metadata、device-code、polling response 与 generated contracts 一致。
- Client core：pending/slow_down/denied/expired/success 状态机和取消行为。
- Tauri：两个入口、设备码显示/复制/二维码、轮询停止、日志脱敏；本机浏览器流程回归。
- Web C：未登录跳转、管理员审批、TOTP、无效/过期设备码、主题二维码和页面溢出。
- E2E：Tauri 模拟设备码申请，Web C 在独立浏览器会话审批，Tauri 轮询并完成 token exchange；
  另保留同设备浏览器授权 E2E。

## 10. 非目标

- 不让 Tauri 接收管理员 Token；
- 不把终端内容或设备码持久化到 Web C 浏览器之外；
- 不删除或改造现有 Bearer Token API；
- 不要求用户配置额外的第三方授权服务；
- 不把设备码流程限定为 Windows，App、EXE、Linux 和未来客户端共用。
