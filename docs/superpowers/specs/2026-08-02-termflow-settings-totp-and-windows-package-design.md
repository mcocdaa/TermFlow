# TermFlow 设置、双重因素认证与 Windows 安装包增量设计

- 日期：2026-08-02
- 状态：已确认，待实施
- 适用范围：共享 `client-ui`、Web C、Tauri C、B Control Plane、默认 Docker Compose 和 Windows 安装包流水线
- 不包含：反向代理、DNS、TLS/mTLS、代码签名证书采购、应用商店发布

## 1. 目标

本次调整把设置页中的部署术语改成用户可理解的产品语言，确保添加电脑和设置页使用同一个
中继服务器公网地址，并把 TOTP 从“扫码确认即强制启用”改为“先绑定验证器，再独立控制登录
保护”。Web C 和 Tauri C 继续复用同一套主题、设置布局和二维码组件。

为了让当前 WSL 开发环境产出的 Tauri 应用可以安装到 Windows，本次同时提供可手动触发的
Windows CI 打包入口。Control Plane Docker 镜像仍不包含 Tauri 源码或安装包。

## 2. 设置页信息架构

设置页页首英文眉题由 `Preferences & Security` 改为 `Settings`，中文标题仍为“设置”。删除
“主题在客户端本地保存；认证和客户端授权由当前 B 管理。”说明，不在产品界面暴露 A/B/C
内部代号。

页面栏目顺序如下：

1. `Appearance / 界面主题`；
2. `Server / 中继服务器`；
3. Web C 显示 `Two Factor Authentication / 双重因素认证`；
4. Web C 继续显示已授权客户端；
5. Tauri C 不获得管理 TOTP 的能力，只显示自己的设备连接信息。

三个现有主题使用铺满设置块内容宽度的等宽响应式网格。主题数量增加时按最小可用宽度自动
换行；桌面端当前三个主题处于同一行且等宽，窄屏自动降为两列或一列。键盘单选语义和方向键
操作保持不变。

## 3. 中继服务器地址

`TERMFLOW_PUBLIC_BASE_URL` 是中继服务器公网地址的唯一权威来源。它表示用户和客户端实际
访问的外部反向代理 URL，不表示 B 自己终止 TLS，也不允许从 `Host` 或转发 header 推断。

B 的 OAuth metadata `issuer`、电脑注册响应中的 `server_url` 和完整 `login_command` 都从该
配置生成。添加电脑弹窗直接展示 B 返回的命令，不再使用 Web 当前 origin 或 Tauri 本地输入值
自行拼接。因此设置页与添加电脑命令必然使用同一个地址。

`Server / 中继服务器` 栏目按以下顺序排版：

- 栏目标题；
- “服务网址”小标题及其后的可点击 QR SVG 图标；
- 只读代码样式的网址框；
- 与网址框同一行的复制按钮。

点击 QR 图标打开模态对话框。二维码只编码版本化连接数据和公开中继地址，不包含注册码、
管理员 Token、TOTP Secret、access token 或 refresh token。二维码前景色和背景色来自当前
主题的语义颜色；切换主题后重新生成。添加电脑或其他需要二维码的界面复用同一个主题二维码
组件，避免重复实现。二维码仍保持足够对比度和浅色静区等可扫描约束，而不是简单套用任意
低对比度颜色。

metadata 或注册请求失败时保留结构化错误，不回退到浏览器地址猜测。复制成功状态可见但不会
改变权威地址。

## 4. 双重因素认证状态模型

TOTP 状态拆分为：

- `configured`：B 已保存通过首个验证码验证的加密 TOTP Secret；
- `enabled`：新 Web session、App/EXE 授权和 CLI token exchange 是否必须提交新 TOTP；
- `available`：B 是否具备安全保存 TOTP Secret 的内部能力，仅用于协议诊断，不在普通界面
  展示主密钥配置细节。

允许的状态为：

| configured | enabled | 产品含义 |
| --- | --- | --- |
| false | false | 尚未绑定验证器 |
| true | false | 已绑定验证器，登录保护关闭 |
| true | true | 已绑定验证器，登录保护开启 |

`configured=false, enabled=true` 是非法状态，仓储层和服务层都不得产生。

关闭“启用双重认证登录”只清除启用时间，不删除加密 Secret。再次开启时
要求管理员 Token 和验证器当前的新验证码。关闭、开启和重新配置都要求管理员 Token 与当前
验证器的新验证码；验证码继续受 counter 防重放和认证限速保护。正常开关只影响之后新建的
Web session、App/EXE 授权和 CLI token exchange，不把当前已授权客户端误当作验证器恢复路径，
也不强制中断正在使用的终端。验证器丢失时没有 Web、App、
恢复码、邮件或短信恢复入口，只能在 Control Plane 容器内执行 reset；reset 才会删除 Secret、
清空配置状态并使认证 epoch 失效。

## 5. 双重因素认证界面和引导

设置页第三栏英文眉题为 `Two Factor Authentication`，中文标题为“双重因素认证”。未配置时
只显示简短用途和“激活双重因素认证”按钮，不显示“TOTP 加密主密钥”等部署术语。

按钮进入 Web C 专用的 `/settings/two-factor-auth` 引导页：

1. 重新输入管理员 Token；
2. 展示主题配色二维码和手工 setup key；
3. 输入验证器生成的第一个 6 位验证码；
4. B 保存已验证的加密 Secret，此时 `configured=true, enabled=false`；
5. 最终步骤展示“启用双重认证登录”开关。开启时要求一个尚未使用的新验证码并将
   `enabled=true`。

设置完成后，设置页显示验证器已绑定状态、登录保护开关和重新配置入口。开关动作需要安全
确认对话框收集管理员 Token 与当前验证码；失败时保持原状态。Tauri C 不注册该管理路由，
也不能调用 Web-only TOTP API。

## 6. 默认 Docker 密钥管理

Web 用户不负责提供或理解 TOTP 加密主密钥。默认单实例 Compose 在持久化 data volume 中首次
启动时以安全随机源原子生成专用主密钥文件，权限限制为仅容器服务用户读取；后续重启复用该
文件。环境变量或 Docker secret 的显式配置优先级更高，现有部署不会被替换密钥。

多 B 部署必须显式提供同一共享主密钥，不能让各实例分别自动生成。自动生成能力只服务默认
单实例 Compose。密钥、TOTP Secret 和二维码内容不得进入日志、镜像层、API 错误或前端持久化。

若底层存储不可写或密钥加载失败，激活 API 返回通用服务不可用错误；普通用户界面提示联系
服务器管理员，不展示具体密钥路径或变量名。

## 7. API 与持久化变化

- 电脑注册响应增加 B 生成的 `server_url` 和 `login_command`；现有 token 和过期时间保留。
- TOTP 状态响应增加 `configured`，保留 `enabled` 和 `available`。
- 首次 setup confirm 只写入加密 Secret 并令 `configured=true, enabled=false`，不自动启用登录
  保护；重新配置替换 Secret 并保持操作前的 enabled 状态。
- 增加 TOTP enable/disable 登录保护操作；两者都执行管理员 Token、当前验证码、限速、审计和
  generation/challenge 失效逻辑，但不撤销已经授权的客户端或当前会话。
- 重新配置以新 Secret 原子替换旧 Secret，旧验证器立即失效。
- 现有数据库字段足以表达“有 ciphertext 但 enabled_at 为空”；迁移只在约束或索引确有需要时
  增加，不复制 Secret。

生成的 TypeScript contracts 继续以 Python protocol models 为唯一来源。共享 UI 只通过
`ClientRuntime` 和 `client-core` API 调用网络、剪贴板与主题能力。

## 8. Windows 安装包

当前 WSL 可以构建 Linux Tauri 目标，但不能把 Linux bundle 当成 Windows 安装包。仓库现有
Windows CI 的 `--no-bundle` 只验证编译，不产生可下载的安装程序。

本次新增手动触发的 Windows GitHub Actions 工作流，在 `windows-latest` 上安装锁定的 Node、
Rust 和 npm 依赖，然后执行：

```powershell
npm run tauri:build --workspace @termflow/tauri-client -- --bundles nsis
```

工作流上传 `apps/clients/tauri/src-tauri/target/release/bundle/nsis/*-setup.exe` 作为短期 CI artifact。
该 unsigned NSIS EXE 可用于当前用户自己的 Windows 测试安装，可能触发 SmartScreen 未知发布者
警告；公开分发前必须另行接入受保护的代码签名证书和发布流程。

如果选择本机 Windows 构建，则必须在 Windows 侧安装 Rust MSVC toolchain、Microsoft C++
Build Tools 的 Desktop development with C++ 工作负载，以及 WebView2，并在 Windows 文件系统
内的仓库副本执行相同命令。本次不在 WSL 内安装交叉编译 NSIS/xwin 工具链，因为官方将该路径
视为有额外限制的后备方案，CI/原生 Windows runner 更可复现。

## 9. 验证

实施时至少覆盖：

- 设置页文案、栏目顺序、主题等宽和响应式行为；
- 三个主题下二维码颜色、弹窗焦点、Escape/遮罩关闭和扫描内容；
- metadata、注册响应和完整登录命令使用同一个 env 地址；
- TOTP `未配置 → 已配置未启用 → 已启用 → 已配置未启用 → 容器 reset` 状态转换；
- 开启、关闭、重新配置的管理员 Token、TOTP 防重放、限速和 session/token 失效；
- Tauri 不获得 Web-only 安全管理能力；
- 默认 Compose 自动生成并重用密钥，显式 secret 覆盖优先且镜像不包含密钥；
- Windows workflow 实际生成并上传 NSIS artifact，常规 Windows 编译门禁继续保留；
- 在隔离的临时 Control Plane、数据卷和端口上进行真实浏览器流程，不改动当前运行实例。

## 10. 兼容和边界

原有 Bearer/CLI/API 兼容策略保持不变，只在 `enabled=true` 时要求 TOTP。终端内容不在 B
持久化，A 的终端尺寸仍是唯一权威。本次不增加反向代理、管理域名、mTLS 处理或官方客户端
真实性判断，也不把 Tauri 安装包放进 Control Plane Docker 镜像。
