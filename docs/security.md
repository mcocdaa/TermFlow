# 安全与隐私

## 权限等价性

向 shell Pane 输入普通文字本质上可以执行命令。Admin 控制权限等同于对这些 tmux
会话的可信终端控制权限；TermFlow 不把已认证控制方的文字做命令沙箱。

## 凭据与授权载体

- Admin Token：管理 B，查看和控制全部 Instance；
- Installation Credential：只能为本机注册 Instance；
- Instance Credential：只能连接并上报一个指定 Instance。

Web C 的浏览器会话和原生客户端 OAuth access/refresh token 是另外两种短期授权载体：它们
都受当前认证 epoch 约束，不能替代 Admin Token 管理 B。原生授权使用 PKCE、DPoP 和系统浏览器
确认；Tauri 页面不把 Admin Token 写入客户端存储。

B 只存 token 的 SHA-256 哈希；Web C 创建注册码时还可以随哈希保存用户指定的非机密
Computer 显示名。注册码最多成功使用一次且默认 60 秒过期；B 在同一次原子消费中取得
显示名，随后才创建 Installation，因此过期或未使用的注册码不会产生 Computer。关闭生成
注册码的 Web 页面不会撤销已经复制出去的码；它仍会在首次成功使用或 B 记录的到期点
失效。A 必须保存可用的 Installation/Instance 原始凭据，因此它们只写入本机明确的
`0600` 文件；父目录为 `0700`，普通模型 repr、stdout 和日志保持遮蔽。

Web C 的登录页把 Admin Token 交换为 8 小时内存会话。启用双重认证后，第一次提交 Token
只会得到短时挑战，必须再提交验证器生成的一次性验证码；成功后才签发会话。浏览器只得到
`HttpOnly`、`SameSite=Strict` Cookie；HTTPS 部署使用 `Secure` 和 `__Host-` 前缀。WebSocket
握手还检查精确 Origin allowlist。Admin Token 不写 localStorage、sessionStorage、URL 或
前端日志。TOTP 关闭时 curl 可直接使用 Admin Bearer Header；TOTP 开启后应先用验证码换取
有 scopes 的 CLI token，再用该 Bearer 访问资源。原生客户端在系统浏览器完成授权后，使用
OAuth access/refresh token 和 DPoP 访问资源。

TOTP 的主密钥只由 B 读取：单实例默认在独立于数据库卷的 `termflow-totp-key` 卷中自动生成
0600 文件（与数据库分离，卷快照不会同时泄露密文与密钥），多实例必须显式共享同一密钥。
验证器丢失不能在 Web C、App 或 EXE 中恢复；必须由拥有 Docker 主机权限的管理员在容器内
运行 `termflow-control auth totp reset`。认证失败和验证码尝试由 B 限速（每来源桶 +
全局桶，全局桶容量由 `TERMFLOW_AUTH_GLOBAL_VERIFICATION_CAPACITY` 控制），挑战过期或
超过尝试次数后不会继续接受该挑战。

凭据轮换：`termflow-control auth rotate` 在保留 TOTP 的情况下推进全局认证 epoch，立即
吊销全部 Web、原生和 CLI 凭据；`auth totp reset` 则同时清除 TOTP。两者都需要容器内本地
确认。修改 `TERMFLOW_ADMIN_TOKEN` 并重启不会自动撤销已签发的凭据，请使用上述命令。

Computer 的注册时间由 B 创建 Installation 时记录，最近在线时间由 B 收到 A 的注册、
心跳或拓扑更新时记录；两者都以 UTC 存储和传输，不依赖 A 的本地时钟。Web C 按当前
访问设备的本地时区显示，不附加 GMT、UTC 或其他时区缩写。不同 C 设备看到的当地钟表
时间可以不同，但对应同一个 B 记录的时间点。

## 本地边界

每个 Instance 使用独立、绝对且不可与默认 tmux 混淆的 `-S <socket>`。能访问这个
socket 的同 OS 用户进程视为可信。`termflow kill` 只操作精确解析出的 Instance UUID、
经过命令行身份核对的 Bridge pid 和该 Instance 的显式 socket。

## 网络与持久化

公网 B 必须使用 HTTPS/WSS；明文 HTTP/WS 只允许 `127.0.0.1`、`localhost` 或 `::1`。
DNS、TLS、反向代理和可选 mTLS 由部署者的外部边缘服务负责，TermFlow 容器只接收代理转发的
HTTP/WS。`TERMFLOW_PUBLIC_BASE_URL` 是用户、A、Web C 和原生客户端共同使用的 canonical
origin；添加电脑返回的登录命令也从它生成。反向代理部署时认证限速默认按直连 IP 计源，
所有用户共享代理 IP 的预算。仅当 B 只能经由可信反向代理访问、且该代理覆写（而不是追加
或保留）`X-Forwarded-For` 时，才可显式设置 `TERMFLOW_TRUST_PROXY=true` 按转发头计源；
HTTP 请求与 WebSocket 连接的认证限速使用同一个来源解析函数，Uvicorn 不做代理头预改写。
非回环的明文 `PUBLIC_BASE_URL` 会在启动时
拒绝；HTTPS 部署自动附加 HSTS 响应头。
token 不放 URL。B 不持久化终端输入、输出、屏幕快照或录像；SQLite 和审计只含身份、
字节数、动作、结果等元数据。A 的短期输出环只存在于 Bridge 内存，进程退出即消失。
终端输入审计（来源会话、累计字节数）以聚合日志写入 stdout 并进入容器日志系统，不落
SQLite；`docker logs` 由 Compose 的 `max-size` 配置轮转。

A 端 `--allow-insecure-http` 会以明文传输注册凭据与终端流量：该开关只输出警告并标记
`status`/日志为 `insecure`，不会拒绝公网 HTTP 目标。仅应在受信任的专用局域网使用；公网
部署必须使用 HTTPS。

每个 Term 同时只有一个可输入的远程 tmux client，新连接显式替换旧连接。单帧最大
64 KiB，并有输入速率、队列和背压上限。远程连接关闭只 detach 代理 client，不能结束
tmux server/session 或 Pane 进程。

原生 C 的所有 HTTP 与 WebSocket 流量都由 Rust 侧统一发出：WebView 不再持有
`http`/`websocket` 插件权限，`native_request_headers`/`native_http_request`/
`native_terminal_connect` 会把目标严格限定为配置 issuer 的 origin 与 `/api/` 路径前缀，
Access Token 不进入 JavaScript 环境；DPoP 签名输入被限定为规范 JWT 结构。客户端日志在
Rust 统一脱敏后落盘。

安装与供应链：Release 产物携带 GitHub Artifact Attestations（build provenance），镜像
以 cosign keyless 签名并附带 SBOM；`install-termflow-node.sh` 在有 GitHub CLI 时校验
attestation。CI 引用完整 commit SHA 且权限最小化。

容器默认只映射 loopback。需要远程访问时，应使用可信反向代理终止 TLS，并保护
Admin Token。不要把数据库、A 配置、Bridge 日志或 tmux socket 上传为诊断附件。

## Agent Broker v0.2.0 的运行时边界与证据等级

参考部署中的 B 不直接操作 Docker。生产运行时门禁由
`HttpHealthRuntimeClient` 对 OpenCode 的 `/global/health` 发起带 Basic Auth 的探测；
绑定的 runtime epoch 仍由 B 校验，探测失败、epoch 不一致、撤销或共享 runtime 冲突均
fail-closed。OpenCode 的 MCP 配置固定在 `deploy/opencode-config.yaml`，只允许审查过的
TermFlow 工具；URL 和 AgentToken 通过 Compose 环境变量的 `{env:}` 插值传入，不把原始
token 写入镜像或持久化卷。普通 B 重启不会自行推进 epoch，撤销或 profile 变更才会使旧
epoch 失效。

验证结果按证据等级区分，不能用较低等级的结果替代较高等级的运行时证明：

| 等级 | 当前覆盖 | 限制 |
| --- | --- | --- |
| 静态/契约 | Python、TypeScript、Rust、Compose 配置、OpenCode fixture、工具 allowlist、`scripts/security/verify-agent-containers.sh` 的可执行检查 | 不证明容器已经启动，也不证明外部模型可用 |
| 确定性产品跨进程 | `tests/e2e/test_agent_product_setup.py` + `test_agent_broker_process.py` 使用真实 A/tmux 和 B 子进程验证 setup 不重启 B、health drift/recovery、旧 epoch/错误 Host/撤销/运行中撤销、SSE replay/dedup、歧义动作 `delivery_unknown`、清理重试和 dead-letter；2026-09-08 组合运行 9 passed，另有 1 个运行中撤销场景通过 | 使用本地 fake OpenCode/provider，不证明真实容器或外部模型 |
| 隔离真实浏览器 | `apps/clients/web/e2e/agent-chat.spec.ts` 与 `agent-terminal.spec.ts` 在 disposable Chromium 中验证登录、Agent 导航、setup、sidecar、隐私/a11y、runtime `503` fail-closed、reload persistence 和单 WebSocket；2026-09-08 desktop 通过，移动 portrait/landscape 亦通过 | 浏览器使用临时无外网环境；未连接稳定 OpenCode/MCP 或外部模型 |
| 容器集成与出口 | `tests/e2e/test_agent_opencode_container.py` 的 pinned 1.18.18 生命周期/inspect smoke（1 passed）以及 `test_agent_provider_egress.py` 的 provider allow/deny、无直连、网络隔离（4 passed，均为 2026-09-08 记录） | 需要 Docker daemon、固定镜像和临时凭据；不覆盖 B/OpenCode/MCP 的真实产品编排或模型行为 |
| live 外部模型 | 2026-09-09 disposable Compose 部署完成真实 B → OpenCode → DeepSeek → approval → Docker A `echo 1` 路径：一次 approval 被消费，重复决定返回 HTTP 409，记录一次 tool start/completion，pane 有一条命令和一条独立输出 `1`，最终 assistant body 恰为 `1`；B/OpenCode 重建后 setup、conversation 和 A 在线状态保留 | 该运行使用 synthetic、明确标注未验证的 provider disclosure metadata，readiness 仍为 `configured_unverified`，所以不证明供应商的 no-training/retention/region 政策或稳定发布就绪；新鲜 re-auth、撤销/断线和 `once`/`reject` 仍是未完成的 release gate |

容器检查脚本只读取明确指定的 Compose project，不执行 up、down 或删除操作。集成测试的
teardown 只清理自身随机 project 和随机 volume 名称。上述边界是覆盖说明，不构成“绝对
安全”保证；宿主机、反向代理、模型供应商和 Docker 默认 seccomp/AppArmor 仍是部署者责任。
