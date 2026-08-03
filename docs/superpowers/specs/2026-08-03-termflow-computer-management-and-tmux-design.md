# TermFlow 电脑管理与 tmux 诊断设计

## 目标

完善电脑管理页的 Computer 删除闭环，保证有在线 Term 时不能删除；让添加电脑在另一台机器成功执行 `termflow login` 后自动结束弹窗、刷新列表并提示“已添加”；修复本地 CLI 对非标准 tmux 版本输出的误判，并保留 tmux 3.2+ 的最低版本要求。

## 现状与边界

- Control Plane 已有 `GET /api/v1/computers`、`GET /api/v1/computers/{id}` 和改名接口，但没有 Computer 删除接口。
- Computer 的 `online` 状态由 `LiveInstanceRegistry` 中是否存在关联 Instance 连接推导；列表中的 Term 也使用同一实时连接状态。
- 添加电脑流程先创建一次性 enrollment token，再由另一台机器执行返回的 `termflow login` 命令完成 Installation 注册；当前弹窗只展示注册码，不感知注册完成。
- Installation 和 Instance 都已有 `revoked_at` 字段，不需要新增迁移；实例删除已有“在线拒绝”的 registry retirement 模式。
- `TmuxRunner` 当前只匹配完整且严格位于 stdout 开头的 `tmux N.N`，诊断信息不足。

## 方案

### Computer 删除

新增 `DELETE /api/v1/computers/{installation_id}`，使用现有 admin 鉴权并返回 `204 No Content`。

处理顺序：

1. 读取该 Installation 及其当前 Instance 列表；不存在或已撤销时返回 `404 computer_not_found`。
2. 为每个关联 Instance 调用 registry 的 retirement 保护。任何 Instance 已在线时返回 `409 computer_online`，并取消本次已建立的 retirement 标记。
3. 在一个 repository 事务中撤销 Installation 凭据、删除其 Instance 记录，并提交；安装 token 随即不能再注册新 Instance，旧 Instance token 也不能重新建立连接。
4. 事务失败时取消 retirement 标记并重新抛出异常。

前端 ComputerTable 增加“操作”列。离线 Computer 显示项目现有 Lucide 图标风格的垃圾桶按钮；在线 Computer 的按钮保留但 disabled，`aria-label` 明确说明“有终端在线时不能删除”。点击可用按钮先确认，确认后调用删除 API；成功移除对应行并在 ComputersView 显示“已删除”，失败显示服务端错误。

### 添加完成检测

EnrollmentDialog 在首次加载 Computer 列表后记录已有 `installation_id` 集合。创建注册码成功后，以约 1 秒间隔轮询 Computer 列表：当出现一个不在基线集合中的 Computer 且 `display_name` 等于本次输入名称时，停止轮询并发出 `added` 事件。轮询只在弹窗打开且注册码有效期间运行；过期注册码继续沿用现有自动刷新逻辑；关闭或卸载时清理所有定时器。

ComputersView 响应 `added`：关闭弹窗、重新加载列表并显示“已添加”。刷新失败时保留已添加提示并单独显示列表加载错误，不能重新打开已完成的注册码内容。

### tmux 版本检测

保留 `tmux 3.2+` 版本门槛。解析器从 stdout 和 stderr 的组合文本中搜索 `tmux` 后的主次版本号，允许前后空白、额外诊断文本和版本输出流变化；低版本继续抛出 `UnsupportedTmuxVersion`。命令返回非零或完全没有版本号时抛出 `TmuxUnavailable`，错误信息包含返回码以及截断后的 stdout/stderr，方便定位 PATH、包装器和发行版差异，不输出用户输入或终端内容。

## 测试策略

- Control Plane API：离线 Computer 删除返回 204、列表不再包含该 Computer、安装凭据失效；关联 Term 在线时返回 409 且数据不变；未知/重复删除返回 404；并发 retirement 失败时不留下阻塞标记。
- Client core：Computer delete 使用 URL 编码的 DELETE 请求并接受 204。
- Client UI：操作列、垃圾桶 SVG、在线禁用态与无障碍文案；确认取消与确认删除；删除成功移除行和消息；添加检测到新 Computer 后关闭弹窗、刷新列表、显示“已添加”；卸载停止轮询。
- Node：标准 stdout、stderr、带前缀/后缀的 tmux 版本输出、低版本、非零返回码和无版本输出；错误诊断内容可见且不含敏感终端数据。
- 完成前运行相关 Vitest/Pytest 以及项目既有验证命令，并区分源码验证与已安装冻结 CLI 的重新打包验证。

## 不在本次范围

- 不改变截图中的深色表格列顺序、排版或整体视觉主题。
- 不新增 enrollment 状态表或额外的长期注册码查询 API。
- 不降低 tmux 最低版本要求，也不自动安装系统 tmux。
