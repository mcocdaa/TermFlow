# TermFlow 统一客户端、交付边界与认证设计

- 日期：2026-08-01
- 状态：已完成讨论确认，等待书面规格审阅
- 适用范围：Web C、Tauri 2 手机/桌面 C、B Control Plane 认证与交付

## 1. 目标与既有边界

本设计在现有 Web C 和完整 tmux 控制能力上，建立未来手机 App 与桌面 App/EXE 的统一
客户端架构，并补齐原生客户端授权、登录防护和可选 TOTP 二步验证。

TermFlow 的正式产品代号保持不变：

- A：运行 tmux、PTY 和主动 Bridge 的 Node；
- B：认证、注册、路由和元数据持久化的 Control Plane；
- C：Web、手机 App、桌面 App/EXE 等所有用户客户端。

讨论登录流程时使用过的 A/B/C 页面编号不属于产品架构。本设计继续遵守以下既有边界：

- A 是终端 cell 尺寸、tmux Session/Window/Pane 和 PTY 生命周期的唯一权威；
- C 可以显示、裁切、平移和缩放，但不能修改 A 的终端尺寸；
- B 不持久化终端输入正文、输出正文或终端录像；
- 手机和桌面原生客户端在产品流程上完全一致，平台差异只存在于系统适配器；
- 本设计覆盖客户端和认证，不改变 A-B Bridge、tmux 控制和终端隐私契约。

本设计补充并在冲突处取代
`docs/superpowers/specs/2026-08-01-termflow-web-control-design.md` 的原生 C 认证假设。Web C
已有行为继续兼容；App/EXE 不再直接持有全局管理员 Bearer Token。

## 2. 目标仓库结构

```text
TermFlow/
├── apps/
│   ├── node/                         # A：Python Node 与 tmux/PTY
│   ├── control-plane/                # B：FastAPI、认证、路由、容器内管理 CLI
│   └── clients/
│       ├── web/                      # Web C 入口与浏览器专用适配器
│       └── tauri/                    # 一个 Tauri 2 工程，覆盖手机与桌面
│           ├── src/                  # Tauri WebView 入口与平台组合
│           └── src-tauri/            # Rust shell、深链、系统浏览器、安全存储
├── packages/
│   ├── protocol/                     # 现有 Python A-B-B-C 协议源
│   ├── design-tokens/                # 现有统一主题与语义 token
│   ├── client-contracts/             # 从公开契约生成的 TypeScript 类型与 codec
│   ├── client-core/                  # 无 Vue/DOM 的认证、API、WS、终端会话核心
│   └── client-ui/                    # Web 与 Tauri 共用的 Vue 页面和组件
├── scripts/
│   └── generate-client-contracts/    # OpenAPI/WS 契约生成与漂移检查
├── deploy/
│   ├── Dockerfile.control-plane
│   └── compose.yaml
├── package.json                      # npm workspaces
└── package-lock.json                 # 全部 TypeScript workspace 的单一锁文件
```

Python 继续使用现有 uv workspace；TypeScript/Vue/Tauri WebView 部分改用根 npm workspace，
避免每个客户端复制依赖、锁文件和构建逻辑。

## 3. 客户端分层与复用规则

### 3.1 `client-contracts`

`packages/protocol` 中的 Pydantic/OpenAPI 与显式 WebSocket schema 是服务端契约源。
`client-contracts` 只包含生成的 TypeScript DTO、错误结构、版本常量和二进制/JSON 帧 codec。
生成文件不得手工修改；CI 重新生成后必须保持工作树无差异。

### 3.2 `client-core`

`client-core` 不导入 Vue、浏览器 DOM 或 Tauri API，负责：

- B 地址与能力发现；
- HTTP 请求、结构化错误和版本协商；
- dashboard、Computer、Term 与终端 WebSocket 状态；
- PKCE、授权事务、短期令牌和 DPoP proof；
- 终端输入、动作和恢复状态机；
- 可注入的时钟、存储、随机数、网络和安全密钥接口。

Web 和 Tauri 通过适配器实现 Cookie、系统浏览器、深链、OS 安全存储等能力。

### 3.3 `client-ui`

`client-ui` 复用 Vue 页面、布局、表单、Computer/Term 组件、xterm.js 终端视图、响应式
移动布局和可访问行为。它只能依赖 `client-core` 暴露的用例接口，不能直接读取 Cookie、
Tauri command 或平台密钥。

`apps/clients/web` 与 `apps/clients/tauri` 是薄组合层：选择对应平台适配器、挂载路由、处理
应用生命周期并产出各自构建物。手机和桌面位于同一个 Tauri 2 工程，不复制业务页面。

## 4. 统一主题

现有 `packages/design-tokens` 是所有 C 的唯一视觉主题源，继续提供：

- `graphite-signal`、`cloud-cobalt`、`midnight-indigo` 三个已实现主题；
- 颜色、间距、圆角、阴影、字体、动效和焦点状态等语义 token；
- xterm.js 背景、前景、ANSI 色板、选区和光标的语义映射；
- TypeScript `ThemeId` 与 CSS theme 文件的一致性测试。

Web 和 Tauri 使用相同 token 和 `client-ui` 组件。V1 主题选择只保存在当前 C 的本地安全或
普通偏好存储中，不同步到 B；凭据、TOTP 和终端数据不能与主题偏好共用存储。

## 5. 构建与交付边界

### 5.1 Control Plane Docker 镜像

Control Plane 镜像只交付：

- `termflow-control-plane` Python wheel 及运行依赖；
- `termflow-protocol` Python wheel；
- Web C 编译后的 `dist`；
- 运行健康检查和容器内管理命令所需的最小系统依赖。

Web builder 只复制根 npm manifest、`apps/clients/web`、`packages/design-tokens`、
`packages/client-contracts`、`packages/client-core` 和 `packages/client-ui`。Python builder 构建 B
与 protocol wheel；最终 runtime 从 builder 复制 wheel/虚拟环境和 Web `dist`。

最终镜像不得包含：

- `apps/node`；
- `apps/clients/tauri`、Rust toolchain、Cargo cache 或 Tauri 安装包；
- Web、共享 UI、测试和 design-token 源码；
- `node_modules`、测试报告、`.git`、`.superpowers` 或本地数据库。

当前 Dockerfile 的 `COPY packages ./packages` 与 `COPY apps ./apps` 是待修正的现状，不是
目标边界。Docker build context 可以是仓库根，但 `.dockerignore` 和精确 `COPY` 决定实际
进入构建层的文件。镜像验收必须检查文件清单，而不能只以构建成功作为证明。

### 5.2 原生客户端

Tauri 2 的桌面安装包由对应 OS 的 CI runner 构建；iOS/Android 由各自签名工具链构建。
它们不通过 Control Plane Docker 镜像发布。代码签名和 Apple App Attest/Google Play
Integrity 可以作为官方发行版的附加信号，但不能成为 B 的基础授权条件，也不能阻止用户
授权兼容的第三方 C。

### 5.3 外部部署边界

域名、DNS、反向代理、TLS 终止，以及可选的 mTLS 证书签发、校验和轮换都属于部署者的入口
设施，不属于 TermFlow。仓库不提供或管理 Nginx/Caddy、CA、服务端证书或客户端证书，B 也不
读取代理转发的客户端证书 header，任何应用认证流程都不能依赖 mTLS 才安全。

Control Plane Compose 继续只提供一个默认绑定宿主机 loopback 的 HTTP 服务。部署者可以按需
在其前面接入反向代理；`TERMFLOW_PUBLIC_BASE_URL` 只描述用户实际访问 B 的 canonical URL，
不代表 B 自己终止 TLS。TermFlow 不增加第二个管理域名，也不把反向代理打包进 Control Plane
镜像或默认 Compose。

## 6. B 地址发现与客户端身份

B 的 canonical URL 由部署者通过 `TERMFLOW_PUBLIC_BASE_URL` 配置，而不是由 Web 管理员在
运行时修改。开发环境默认是 `http://127.0.0.1:8765`；生产部署者把它设置为经自己的入口设施
公开的 HTTPS 地址，例如 `https://termflow.example.com`。B 只用该配置生成 issuer、授权链接、
回调和二维码，不根据请求的 `Host`、`Forwarded` 或 `X-Forwarded-*` header 猜测公开地址。
`TERMFLOW_TRUSTED_WEB_ORIGINS` 同样是部署配置，不是管理页设置。

Web C 从同源 metadata 的 `issuer` 读取并只读展示该地址，提供复制和生成连接二维码，不提供
修改入口。原生 C 首次连接 B 有两条入口：

1. 用户手工输入 HTTPS B 地址；
2. 用户在已认证 Web C 中生成连接二维码并扫描。

二维码可以包含 canonical URL、一次性授权事务标识和服务器显示信息，但绝不包含管理员
Token、TOTP Secret、access token 或 refresh token。C 从该 URL 下固定的公开 metadata
endpoint 读取 issuer、协议版本、授权端点和能力。域名如何解析、HTTPS 在哪里终止以及部署者
是否额外启用 mTLS，不进入 C/B 协议。

B 不接受“我是官方 C”作为授权证明。任何原生 C 都被视为不能保守静态 client secret 的
公开客户端。B 信任的是：管理员在系统浏览器明确批准了某个客户端实例的公钥，且后续请求
持续证明持有对应私钥。

## 7. 凭据类别与权限

| 凭据 | 持有者 | 生命周期 | 用途 |
| --- | --- | --- | --- |
| Bootstrap 管理员 Token | B 部署者 | 长期、可轮换 | Web/CLI 主凭据，不进入 App/EXE，也不承担 TOTP 恢复 |
| Web session Cookie | 浏览器 | 默认 8 小时 | Web C HTTP/WS；HttpOnly、Secure、SameSite=Strict |
| Authorization code | 浏览器与发起 C | 默认 60 秒、一次性 | 完成 PKCE 原生授权 |
| Client private key | 单个原生 C | 直到设备移除 | DPoP、设备身份与 token 绑定；不得导出 |
| Access token | Web 以外的 C/CLI | 默认 10 分钟 | 最小 scope、audience 限制的 API/WS 访问 |
| Refresh token | 单个原生 C | 轮换、可撤销 | 获取新 access token，并绑定客户端公钥 |
| TOTP Secret | B 与用户验证器 | 用户关闭/重置前 | 可选的第二步验证码生成与验证 |

每个原生客户端实例在 B 中拥有独立记录：显示名称、平台、版本、公钥、授权 scope、创建时间、
最近使用时间和撤销时间。撤销一个客户端不能影响其他客户端或 A 的 Installation Credential。

## 8. Web C 登录

Web 登录保留“管理员 Token 只输入一次并换取 HttpOnly Cookie”的既有体验，并增加统一防护：

1. Web C 向同源 B 提交管理员 Token；
2. B 只有在 Token 正确时才透露需要 TOTP，并返回短期、不透明登录 challenge；
3. TOTP 关闭时直接创建 session；启用时进入 6 位验证码页；
4. challenge 与验证码通过后创建 session，立即清空页面内主凭据和验证码；
5. Cookie 不进入 localStorage、sessionStorage、IndexedDB、URL 或应用日志；
6. 状态变更和 WebSocket 继续校验精确 Origin。

登录失败使用统一错误，不通过状态码、正文或明显时序暴露管理员 Token 是否正确或 TOTP 是否
启用。B 重启仍可以使进程内 Web session 失效；以后若扩展多 B，再把 session/revocation
状态迁到共享存储。

## 9. 手机与桌面原生 C 登录

手机与桌面共用同一个状态机：

1. C 获取或输入 B 地址；
2. C 在 OS 安全存储中生成客户端密钥对，同时生成 PKCE verifier/challenge；
3. C 使用系统浏览器打开 B authorization endpoint；
4. B 在浏览器中执行管理员认证，并在 TOTP 启用时要求本次授权输入新验证码；已有 Web
   session 不能跳过这次 TOTP step-up；
5. 浏览器展示 B 地址、客户端名称、平台、公钥指纹和 scope，由用户明确允许或拒绝；
6. B 返回一次性 authorization code；
7. C 使用 code、PKCE verifier 和客户端公钥换取 DPoP-bound access/refresh token；
8. 后续 HTTP 与 WebSocket 请求携带短期 token 和绑定 method、URL、时间、nonce 的 DPoP proof。

移动平台优先使用 claimed HTTPS App/Universal Link；桌面可以使用 Tauri deep link 或临时
loopback 回调。回调只是平台传输适配器，不改变用户看到的登录步骤。PKCE 使截获 code 的
其他本机进程无法单独兑换令牌。

原生 C 只保存自己的私钥和设备 refresh token，不保存管理员 Token、TOTP Secret 或浏览器
Cookie。安全存储接口优先使用 Keychain/Keystore/TPM 等 OS 能力；平台无法提供不可导出密钥
时，使用独立加密 vault 并在客户端记录较低的设备保障等级。

## 10. curl 与 Bearer 兼容

公开 API 继续使用 Bearer scheme，避免破坏 curl 和未来兼容客户端，但不再要求 App/EXE
持有全局管理员 Token。

- TOTP 关闭时，现有管理员 Bearer 兼容模式可以继续工作；
- TOTP 启用后，全局管理员 Token 只能用于 Web/CLI 登录交换，不能单独访问受保护资源；
- CLI 登录使用管理员 Token，并在需要时输入 TOTP，换取短期、限 scope 的 CLI access token；
- API 请求和 WebSocket 使用该短期 Bearer，过期后重新登录；
- 已签发 token 可以按客户端/CLI session 单独撤销。

因此 TOTP 不会被旧的静态 Bearer 路径绕过，同时保留 API 的标准 Authorization 形式。

## 11. 登录限速与抗自动化

高熵管理员 Token 必须由密码学安全随机源生成，部署校验拒绝明显过短的值。限速仍作为资源
保护、误配置保护和 TOTP 防猜的一部分：

- B 对登录、authorization、token 与 WebSocket 握手实施限制；
- 每个来源默认允许突发 5 次，之后按每分钟 1 次恢复；
- 连续失败使用从 1 秒递增到最长 300 秒的等待；
- 单个登录、授权或 TOTP challenge 最多允许 5 次错误，超过后立即作废；
- B 对认证端点设置全局请求和并发预算，避免分布式请求耗尽服务；
- `429` 携带 `Retry-After`，但认证错误正文保持通用；
- 成功认证后重置对应失败状态；失败、限速和重置写审计，任何日志都不能包含提交的凭据。

不能采用永久或全局管理员硬锁定，以免公网攻击者锁死唯一管理员。验证码 challenge 的作废
不影响 Docker 内恢复命令。

## 12. 可选 TOTP 二步验证

### 12.1 配置与正常使用

TOTP 默认关闭，只能通过已认证 Web C 的“设置 → 安全”启用：

1. 用户重新提交当前管理员 Token；
2. B 使用密码学安全随机源生成独立的 160-bit TOTP Secret，并显示标准
   `otpauth://totp/...` 二维码与手工 setup key；
3. 用户使用自己的兼容验证器 App 扫描；验证器不需要连接 TermFlow 或第三方服务；
4. 用户输入第一个有效验证码；验证通过前 B 不把 TOTP 标记为 enabled；
5. 启用后，每次新建 Web session、App/EXE authorization 和 CLI token exchange 都要求
   一个新的 TOTP；已有验证码不能为第二个登录重复使用。

V1 使用兼容性最广的 TOTP 参数：HMAC-SHA-1、6 位、30 秒。B 最多接受当前时间步和一个相邻
时间步，并记录最后成功使用的 counter，禁止同一 counter 再次成功。错误尝试受第 11 节限速。

正常关闭或重新配置 TOTP 只能在 Web C 中执行，并要求管理员 Token 与当前有效 TOTP。重新
配置生成全新 Secret，旧 Secret 立即失效。Web C 不生成或展示恢复码。

### 12.2 Secret 存储

B 必须计算预期 TOTP，因此 TOTP Secret 不能只存哈希。数据库只保存 AEAD 加密后的 ciphertext、
nonce、算法版本、启用时间和最近成功 counter；加密主密钥由独立 Docker secret 或环境 secret
提供，不能从管理员 Token 派生，也不能写进数据库、镜像或日志。

没有配置 TOTP 加密主密钥时，Web C 显示该能力不可用并提示服务器管理员配置；B 仍可在
TOTP 关闭状态正常运行。多 B 部署必须共享同一主密钥并原子更新最近成功 counter。

### 12.3 唯一恢复路径

验证器丢失时没有客户端恢复流程、恢复码、邮件、短信或远程 reset API。唯一恢复路径是拥有
Docker 主机权限的服务器管理员进入 control-plane 容器执行本地命令：

```bash
docker compose exec control-plane termflow-control auth totp reset
```

命令要求交互确认，并在单个事务中：

1. 删除加密 TOTP Secret 并关闭 TOTP；
2. 增加全局认证 epoch，使所有 Web session、App/EXE access/refresh token、CLI token 和待处理
   challenge 立即失效；
3. 保留客户端登记、显示名称和公钥，但这些客户端必须重新登录；
4. 写入不含 Secret 的服务器审计记录。

该命令属于 B 运维面，与所有 C 无关，也不通过 Control Plane HTTP/OpenAPI 暴露。

## 13. 隐私与错误处理

- TOTP Secret、setup QR、管理员 Token、authorization code、access/refresh token、DPoP 私钥和
  Cookie 不能进入日志、遥测、URL、错误正文或终端交互统计；
- TOTP setup 响应使用 `Cache-Control: no-store`，Secret 只在未确认的 setup 事务中展示；
- 授权码过期、PKCE 失败、回调被取消和 TOTP challenge 作废都返回可重试的结构化错误；
- 原生 C 离线时继续展示已缓存的非敏感 UI 状态，但不能伪造登录成功或发送终端输入；
- TOTP 是短时 OTP，但人工输入不具备抗实时钓鱼能力；生产环境的 HTTPS server identity 和
  正确 B 地址仍是必要边界，Passkey/WebAuthn 可作为以后新增的抗钓鱼认证器。

## 14. API 与持久化边界

公开认证路径固定为：

```text
GET    /.well-known/oauth-authorization-server

POST   /api/v1/admin/sessions
POST   /api/v1/admin/sessions/{challenge_id}/totp
GET    /api/v1/admin/session
DELETE /api/v1/admin/session

GET    /api/v1/oauth/authorize
POST   /api/v1/oauth/token
POST   /api/v1/oauth/revoke

POST   /api/v1/admin/cli-tokens

GET    /api/v1/admin/totp
POST   /api/v1/admin/totp/setups
POST   /api/v1/admin/totp/setups/{setup_id}/confirm
DELETE /api/v1/admin/totp

GET    /api/v1/admin/clients
PATCH  /api/v1/admin/clients/{client_id}
DELETE /api/v1/admin/clients/{client_id}
```

`POST /api/v1/admin/sessions` 在 TOTP 关闭时保持现有 `201 + Cookie` 行为；TOTP 开启且管理员
Token 正确时返回 `202 + challenge_id`，再由第二个路径完成 session。无效主凭据永远不返回
challenge。TOTP 和客户端管理路径只接受 Web Cookie、精确 Origin 和必要的重新认证，原生 C
不能调用 TOTP 配置路径。

OAuth metadata 声明 authorization、token、revoke、PKCE S256 和 DPoP 能力。authorization
endpoint 的登录、TOTP step-up 和 consent 是浏览器页面；token/revoke endpoint 供原生 C
直接调用。CLI token endpoint 使用主凭据和可选 TOTP 签发短期 Bearer。DPoP nonce/proof
验证是 token endpoint 和现有受保护 HTTP/WS 资源的横切认证层，不另设弱化的兼容端点。

数据库新增认证器、原生客户端、refresh/CLI token digest、TOTP ciphertext、authorization
challenge 和 auth epoch。B 只保存 token digest 或加密后的必要 Secret，不保存 access token
明文。终端内容与这些认证表完全分离。

## 15. 测试与验收

### 15.1 契约与共享客户端

- 生成的 TypeScript 契约与 Python OpenAPI/WS schema 无漂移；
- `client-core` 在无 DOM 环境覆盖 PKCE、DPoP、token rotation、重试和状态转换；
- 同一组 UI contract test 在 Web 与 Tauri WebView 组合层运行；
- 三个主题在 Web、手机尺寸和桌面尺寸下使用相同语义 token 和 xterm 色板。

### 15.2 认证安全

- 无效管理员 Token 不暴露 TOTP 状态；
- authorization code、TOTP counter 与 refresh token 均不能成功重放；
- 同一设备公钥与 token 绑定，复制 token 到另一密钥后失败；
- challenge 第 6 次尝试前已作废，限速返回稳定 `Retry-After`；
- 启用 TOTP 后所有静态管理员 Bearer 受保护资源请求失败；
- TOTP reset 只能由容器 CLI 调用，并注销全部会话但保留客户端登记；
- 日志、响应、数据库 repr 和浏览器持久化中不存在任何原始凭据。

### 15.3 Docker 与交付

- Web production build、Python wheel 和 Control Plane 镜像可重复构建；
- 最终镜像文件清单不包含 A、Tauri、Cargo、前端源码、测试和完整 monorepo；
- Compose 健康检查、Web SPA fallback、容器内 TOTP reset CLI 和 A 自动重连均验证；
- Tauri 桌面与手机构建独立于 Docker，且共享页面的功能/主题测试通过。

## 16. 实施顺序与非目标

后续实施计划按以下依赖顺序拆分：

1. 固化公开契约生成、npm workspace 和客户端共享层；
2. 修正 Control Plane Docker 构建边界，保持现有 Web C 行为不变；
3. 实现 B 登录限速、认证持久化、原生 OAuth 风格授权、DPoP 和客户端管理；
4. 实现 Web C 安全设置与 TOTP，以及容器内 reset 命令；
5. 将现有 Web 页面迁入 `client-ui/client-core`，由薄 Web 入口回归验证；
6. 创建一个 Tauri 2 工程，先验证桌面壳，再复用到 iOS/Android；
7. 完成签名、安装包、移动生命周期和跨平台真实设备验收。

本设计不包含：反向代理的选择、配置或打包，域名/DNS 管理，TLS/mTLS 证书生命周期，自建账号
系统、邮件/短信恢复、TermFlow 推送审批、云端 TOTP 服务、强制 Apple/Google attestation、
B 持久化终端内容、C 修改 A 终端尺寸或本阶段引入多 B 消息总线。
