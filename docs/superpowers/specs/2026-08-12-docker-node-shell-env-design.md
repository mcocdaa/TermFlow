# Docker A Term shell 环境变量设计

## 目标

让 Web C 或 `termflow attach` 进入 Docker A 的 tmux Term 时，shell 可由容器环境变量选择：

- 不设置 `TERMFLOW_SHELL` 时使用 Bash。
- `TERMFLOW_SHELL=bash` 时使用 `/bin/bash`。
- `TERMFLOW_SHELL=sh` 时使用 `/bin/sh`。
- 其他值视为配置错误，容器在创建登录态或启动 `termflow serve` 前失败，并输出允许值。

该配置只属于 Docker A，不改变原生安装的 Computer A、Control Plane B、Web C 或
`docker exec` 显式执行的命令。

## 当前行为与原因

Docker A 设置 `TERMFLOW_NEW` 后，`deploy/entrypoint.node.sh` 会用
`termflow serve --name ...` 替换镜像的 `CMD ["bash", "-l"]`。随后 Python 代码创建
tmux session，但没有传入初始命令。因此，镜像 `CMD` 不能决定 Web C 内看到的 Term
shell；该 shell 由新启动的 tmux server 根据进程环境选择。

## 设计

`deploy/entrypoint.node.sh` 在降权完成后、自动登录和 `termflow serve` 之前解析
`TERMFLOW_SHELL`。入口脚本将允许值映射到绝对路径并导出标准 `SHELL`：

| `TERMFLOW_SHELL` | 导出的 `SHELL` |
|---|---|
| 未设置或空值 | `/bin/bash` |
| `bash` | `/bin/bash` |
| `sh` | `/bin/sh` |

不用用户输入直接构造命令，也不接受任意路径。这样 tmux server 仍由现有 Python
生命周期创建，Docker 专属策略留在镜像入口，非 Docker A 的 shell 行为不变。

容器启动时，入口脚本先完成两个固定挂载点的属主初始化并降权，再校验 shell 配置。
非法值会在任何网络登录或 tmux/Bridge 创建之前退出。合法值随 `termflow serve` 继承到
tmux；新 session 的首个 pane 使用所选 shell，其后由 tmux 创建的新 window/pane 沿用
同一个默认 shell。

## 生命周期语义

环境变量属于容器配置。修改 `TERMFLOW_SHELL` 后，用户必须重新创建 Docker A 容器。
容器重建会停止原进程并由 `termflow serve` 恢复同一 Term 身份、创建新的 tmux server；
恢复后的 pane 使用新 shell。运行中的 pane 不支持热切换，也不承诺保留其中的进程。

`/home/termflow` 身份盘与 `/work` 用户数据盘语义不变。shell 选择不写入身份卷，不影响
升级或回退到未实现此变量的旧镜像。

## 用户接口和文档

README 的 Docker A 示例保持默认 Bash。自定义时在 `docker run` 中增加：

```bash
--env TERMFLOW_SHELL=sh
```

文档同时列出仅支持 `bash`、`sh`，以及修改后必须重建容器。

## 验证

- 入口脚本契约测试覆盖未设置、`bash`、`sh` 和非法值。
- Node 镜像验证分别启动默认配置与 `TERMFLOW_SHELL=sh`，在 tmux pane 内读取
  `#{pane_current_command}` 或执行不依赖交互提示符的命令，确认实际 shell 分别为
  `bash` 和 `sh`。
- 非法值验证容器非零退出，且错误信息不包含未经处理的命令执行行为。
- 运行现有 Node 单元测试、镜像验证与文档契约测试，确认 tmux 生命周期、非 root
  运行和只读 rootfs 约束没有回归。

## 非目标

- 不支持 `/usr/bin/zsh` 等任意路径或镜像内未安装的 shell。
- 不增加 Web C 设置项，也不允许每个 Term、window 或 pane 单独选择 shell。
- 不改变 `termflow new`、`termflow attach` 或 `termflow serve` 的 CLI 参数。
- 不通过修改 Docker `CMD` 实现 tmux shell 配置。
