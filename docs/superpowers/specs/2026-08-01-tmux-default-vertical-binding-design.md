# tmux 默认上下切分快捷键识别设计

## 问题

tmux 官方默认绑定为：

- `C-b %`：左右排列两个 Pane，对应 `split-window -h`。
- `C-b "`：上下排列两个 Pane，对应不带方向参数的 `split-window`。

TermFlow A 当前只在命令包含 `-h` 或 `-v` 时识别切分动作。因此默认 `%` 能识别为 `split_left_right`，默认 `"` 却被上报为 `split_top_bottom: null`。B 只是透传该快照，C 因而在“上下切分 Pane”悬浮态显示“未绑定”。两个当前运行的 Term 均已用各自私有 tmux socket 复现。

## 设计

只修改 A 的 `termflow_node.tmux.bindings._semantic_action`：

1. `split-window` 包含 `-h` 时识别为 `split_left_right`。
2. `split-window` 包含 `-v` 时识别为 `split_top_bottom`。
3. `split-window` 未指定 `-h` 时按 tmux 的默认垂直切分语义识别为 `split_top_bottom`。

第三条保留对用户自定义键的支持：按键仍来自 `tmux list-keys -T prefix`，不会在 C 硬编码 `C-b "`。如果用户真正解绑该动作，列表中没有对应命令，A 仍上报 `null`。

## 验证

- 单元测试使用 tmux 真实默认转义格式 `bind-key -T prefix \" split-window`，先证明旧实现把上下切分判为未绑定，再验证修复后快照为 `C-a "`。
- 保留显式 `split-window -v` 的覆盖，避免破坏自定义配置。
- 对现有 Term 的私有 socket 重新读取 binding snapshot，验证 A 能检测到 `C-b "`。
- C 不需要代码改动；更新 A 后重新连接终端即可收到正确悬浮提示。

## 参考

- tmux 官方 Getting Started：<https://github.com/tmux/tmux/wiki/Getting-Started#splitting-the-window>
