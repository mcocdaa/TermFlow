# TermFlow Compose 环境变量与 TOTP 设置密钥浮层设计

日期：2026-08-03

## 背景

当前生产 Compose 把完整 Docker 镜像地址作为必填的 `TERMFLOW_IMAGE`，把仓库所有者、镜像仓库和发布 tag 暴露成普通部署配置。这会让 Fork 无法仅凭源码自然构建，也把“镜像从哪里分发”错误地归入 TermFlow 运行时配置。

同一份 Compose 还要求部署者同时填写 `TERMFLOW_PUBLIC_BASE_URL` 与通常完全相同的 `TERMFLOW_TRUSTED_WEB_ORIGINS`。B 的配置层已经能够在未显式提供可信 Origin 时使用公开网址，因此普通同源部署不需要这份重复配置。

TOTP 绑定页面目前把设置密钥直接展开到二维码下方，导致页面重排。用户要求它改为触发按钮附近的小型浮层。

## 目标

- 默认 Compose 从当前 checkout 的源码构建 B 与 Web C，不指定远程镜像来源。
- Fork 和 GitHub Actions 都直接以仓库 Dockerfile 为构建入口。
- 普通同源部署只配置一个公开服务网址。
- `.env.example` 用中文解释所有公开的运行限制变量。
- `.env.example` 保留可选的 `TERMFLOW_TOTP_MASTER_KEY`，明确其生成、优先级和不可随意更改的后果。
- TOTP 设置密钥使用锚定在触发按钮附近的小型浮层，不再改变绑定页面布局。

## 非目标

- 不在 Compose 中决定镜像推送到 GitHub、GHCR 或其他 Registry。
- 不实现 TOTP 主密钥在线轮换或现有密文重新加密。
- 不改变 TOTP 验证算法、二维码内容或绑定 API。
- 不新增 Web C 与 B 跨域部署的产品界面。
- 不修改反向代理、TLS 或 mTLS 边界。

## Compose 构建边界

`deploy/compose.yaml` 删除 `TERMFLOW_IMAGE` 插值，直接声明：

```yaml
build:
  context: ..
  dockerfile: deploy/Dockerfile.control-plane
```

默认部署命令为：

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Compose 不硬编码 GitHub 用户名、Registry 或发布 tag。GitHub Actions、Fork 维护者和其他发布系统可以直接调用同一 Dockerfile，自行决定本地 tag、远程仓库和推送策略。现有依赖 `TERMFLOW_IMAGE` 的发布验证脚本与文档需要改为直接验证调用者传入的已构建镜像，不能重新把镜像来源塞回普通 `.env`。

## 公开网址与浏览器 Origin

`TERMFLOW_PUBLIC_BASE_URL` 是普通部署中唯一需要填写的服务网址。它继续用于：

- Web C 设置页显示的中继服务器网址；
- 添加电脑生成的 `termflow login --server ...` 命令；
- OAuth issuer 与授权 URL；
- DPoP `htu` 的 canonical URL；
- 浏览器 Cookie 的 HTTPS/loopback 策略。

普通 Compose 删除 `TERMFLOW_TRUSTED_WEB_ORIGINS`。B 保留已有安全行为：未显式设置可信 Origin 时，精确使用 `TERMFLOW_PUBLIC_BASE_URL` 的 scheme、host 和 port。浏览器 Cookie 的状态修改请求和浏览器 WebSocket 继续校验浏览器自动发送的 `Origin`；Bearer/DPoP 原生客户端不依赖这个浏览器机制。

底层 `Settings.trusted_web_origins` 能力暂时保留，供非标准跨域部署通过自定义环境或 Compose override 使用，但它不出现在普通 `.env.example`。

## TOTP 主密钥

`TERMFLOW_TOTP_MASTER_KEY` 是服务器端 32 字节、无填充 base64url 编码的加密主密钥。B 使用它建立 AES-GCM secret box，加密数据库中的验证器设置密钥、临时 TOTP 设置数据和需要持久化的认证上下文。

它不是用户六位验证码，也不是展示给验证器 App 的 Base32 设置密钥。修改它不会“刷新”2FA；在没有密文迁移的情况下，新值无法解密旧值，会使已有验证器绑定不可用。

`.env.example` 使用注释形式公开这个可选项：

```dotenv
# 可选：单 B 默认在 termflow-data 中自动创建并复用私有主密钥。
# 仅在多 B 共享密钥或明确管理密钥材料时设置；完成 2FA 绑定后不可直接更换。
# 生成：python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
# TERMFLOW_TOTP_MASTER_KEY=replace-with-generated-base64url-key
```

普通 Compose 保留可选的原始环境变量传入，并同时保留 `/app/data/totp-master-key` 自动文件。解析优先级不变：显式 `TERMFLOW_TOTP_MASTER_KEY` 优先；未设置时使用数据卷自动文件。多 B 或 Docker secret 场景仍可使用现有 `compose.totp-secret.yaml` 文件注入方式。

## `.env.example` 文档层级

示例文件按以下顺序组织：

1. 必填安全配置：`TERMFLOW_ADMIN_TOKEN`。
2. 网络入口：`TERMFLOW_HOST_PORT`、`TERMFLOW_PUBLIC_BASE_URL`、本地 loopback 开关。
3. 可选 TOTP 主密钥及不可轮换警告。
4. 会话与注册码有效期。
5. 终端输入和队列保护限制。

每个可调变量的中文注释必须说明单位、默认值、影响对象和何时需要调整。Compose 已有安全默认值的变量不因此变成必填项。

## 设置密钥小型浮层

“无法扫描？使用设置密钥”仍位于主题二维码下方。点击后在按钮附近打开非模态小型浮层：

- 浮层标题为“设置密钥”；
- 使用等宽字体显示完整设置密钥；
- 提供“复制密钥”按钮和复制完成反馈；
- 不插入文档流，不改变二维码、表单或卡片位置；
- 优先显示在触发按钮下方，并限制在卡片和视口宽度内；
- 窄屏时宽度不超过可用视口；
- 点击触发按钮切换开关状态；
- 点击浮层外部或按 Escape 关闭；
- 关闭后焦点回到触发按钮；
- 开始新的 TOTP 设置、确认绑定或离开组件时清理打开和复制反馈状态。

触发按钮使用 `aria-haspopup="dialog"`、`aria-expanded` 和 `aria-controls`。浮层使用 `role="dialog"` 与可访问标题，但不使用 `aria-modal="true"`，因为它不会遮挡或锁住页面其他内容。

## 测试与验收

配置和部署契约测试需要证明：

- 普通 Compose 不再包含或要求 `TERMFLOW_IMAGE`；
- 普通 Compose 包含源码构建上下文和 Dockerfile；
- `.env.example` 不包含 `TERMFLOW_TRUSTED_WEB_ORIGINS`；
- B 在未配置可信 Origin 时使用公开网址，并继续拒绝不匹配 Origin；
- `.env.example` 包含带安全警告的可选 TOTP 主密钥示例；
- `.env.example` 对会话、注册码和终端限制变量都有中文用途说明。

组件与浏览器测试需要证明：

- 设置密钥默认不在 DOM 中；
- 点击触发按钮显示邻近浮层，页面关键布局坐标不变；
- 复制按钮写入完整密钥并显示反馈；
- Escape、外部点击和再次点击触发按钮都能关闭；
- 关闭后恢复触发按钮焦点；
- 桌面和窄屏下浮层不超出视口；
- 完整 TOTP 绑定流程仍然通过。

最终验证包括部署契约测试、B 配置和 Origin 安全测试、共享 UI 单元测试、类型检查、Web 生产构建，以及隔离的已认证浏览器 TOTP 流程。
