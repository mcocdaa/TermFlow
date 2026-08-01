# 安全与隐私

## 权限等价性

向 shell Pane 输入普通文字本质上可以执行命令。Admin 控制权限等同于对这些 tmux
会话的可信终端控制权限；TermFlow 不把已认证控制方的文字做命令沙箱。

## 三类凭据

- Admin Token：管理 B，查看和控制全部 Instance；
- Installation Credential：只能为本机注册 Instance；
- Instance Credential：只能连接并上报一个指定 Instance。

B 只存 token 的 SHA-256 哈希；注册码单次使用且十分钟过期。A 必须保存可用的
Installation/Instance 原始凭据，因此它们只写入本机明确的 `0600` 文件；父目录为
`0700`，普通模型 repr、stdout 和日志保持遮蔽。

## 本地边界

每个 Instance 使用独立、绝对且不可与默认 tmux 混淆的 `-S <socket>`。能访问这个
socket 的同 OS 用户进程视为可信。`termflow kill` 只操作精确解析出的 Instance UUID、
经过命令行身份核对的 Bridge pid 和该 Instance 的显式 socket。

## 网络与持久化

公网 B 必须使用 HTTPS/WSS；明文 HTTP/WS 只允许 `127.0.0.1`、`localhost` 或 `::1`。
token 不放 URL。B 的 SQLite 和审计只含身份/字节数/结果等元数据；Pane 输入、输出、
屏幕快照和断线命令不持久化。A 的 Pane 输出环只存在于 Bridge 内存，进程退出即消失。

容器默认只映射 loopback。需要远程访问时，应使用可信反向代理终止 TLS，并保护
Admin Token。不要把数据库、A 配置、Bridge 日志或 tmux socket 上传为诊断附件。
