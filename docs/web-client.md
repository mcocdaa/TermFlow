# Web C 使用与主题

Web C 是独立客户端，只调用 B 的公开 `/api/v1` HTTP 和 WebSocket 契约。它和 B 构建进
同一个 Docker 镜像是为了简化部署，不代表它可以读取 B 的 Python 模块或 SQLite。

## 页面

- 登录：使用不带全局导航的极简页面；Admin Token 只用于换取 HttpOnly 会话，成功后立即
  从组件状态清除；
- 总览：显示在线 Term、活动 Pane、24 小时交互数和 Computer 卡片；在线 Term 和活动 Pane
  指标可悬浮或聚焦查看解释。每个 Computer 卡片内的 Term 使用独立圆角卡片，在线 Term
  整块可点击进入终端；
- 电脑管理：使用“名称 / 终端 / 最近在线 / 注册时间”四列；点击 Computer 名称修改显示名。
  添加电脑时先输入名称，再创建 60 秒有效的一次性注册码；A 消费注册码时 B 将该名称应用
  为显示名，同时保留 A 上报的真实 hostname。注册码过期时，保持打开的弹窗使用同一名称
  自动换新；
- Term：显示 A 上真实 tmux client，支持完整键盘、IME、粘贴、状态栏、Window/Pane 与
  copy mode。

列表只显示用户的 Term 名称、Window/Pane 名称和 tmux 原始 `pane_current_command`。
TermFlow 不根据进程名硬编码 Codex、Claude Code 或其他 Agent 品牌，用户可以自由重命名
tmux Session。

## 桌面显示

标题栏中的单个显示按钮提供竖排的 50%、75%、100% 和适应窗口选项。Lucide 圆形 SVG
表示当前选择。旁边的 tmux 动作菜单可展开 split、新 Window、Pane 导航、zoom、copy
mode 与关闭 Pane；“显示”“tmux 操作”“聚焦 Pane”只在点击后展开，并通过按钮高亮与
Chevron 方向反馈当前状态。菜单浮在 terminal 上方，不占用 A 的字符行。动作项悬浮或
键盘聚焦时显示 A 实际配置的 tmux Prefix/快捷键，而不是假设一定是 Ctrl+B。

适应窗口只等比缩放 A 报告的固定 rows/cols，不修改 A 的终端尺寸；该模式完整容纳远端
网格且没有页面或终端框滚动条。50%、75% 和 100% 模式需要时只在终端区域内部滚动。

注册和最近在线时间均由 B 以 UTC 记录。Web C 按访问设备的浏览器本地时区格式化，不显示
GMT、UTC 或其他时区后缀；B、A 和 C 位于不同时区时仍保持同一个时间点。

## 手机与横屏

手机支持双指缩放、单指拖动和基于拓扑几何的“聚焦 Pane”。这些只改变本地 viewport，
不会改变 A 的 rows/cols，也不会自动触发 tmux zoom。横屏沿用与竖屏相同的缩放和操作
逻辑。Ctrl、Alt、Shift、Esc、Tab、tmux Prefix 和常用动作位于可点击展开/收起的悬浮栏；
关闭 Pane 必须二次确认。

## 主题

首版提供三个完整主题：`graphite-signal`（默认）、`cloud-cobalt` 和
`midnight-indigo`。所有页面、菜单、状态、terminal 外框和焦点环使用同一组语义 design
tokens。浏览器只持久化主题标识，不持久化凭据或终端正文；未来 App、EXE 和 Linux 安装
包应复用相同 token 名称，而不是复制组件中的颜色值。
