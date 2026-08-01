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

Web C 的登录页把 Admin Token 交换为 8 小时内存会话。浏览器只得到 `HttpOnly`、
`SameSite=Strict` Cookie；HTTPS 部署使用 `Secure` 和 `__Host-` 前缀。WebSocket 握手还
检查精确 Origin allowlist。Admin Token 不写 localStorage、sessionStorage、URL 或前端
日志。curl 和原生客户端仍可使用 Bearer Header。

## 本地边界

每个 Instance 使用独立、绝对且不可与默认 tmux 混淆的 `-S <socket>`。能访问这个
socket 的同 OS 用户进程视为可信。`termflow kill` 只操作精确解析出的 Instance UUID、
经过命令行身份核对的 Bridge pid 和该 Instance 的显式 socket。

## 网络与持久化

公网 B 必须使用 HTTPS/WSS；明文 HTTP/WS 只允许 `127.0.0.1`、`localhost` 或 `::1`。
token 不放 URL。B 不持久化终端输入、输出、屏幕快照或录像；SQLite 和审计只含身份、
字节数、动作、结果等元数据。A 的短期输出环只存在于 Bridge 内存，进程退出即消失。

每个 Term 同时只有一个可输入的远程 tmux client，新连接显式替换旧连接。单帧最大
64 KiB，并有输入速率、队列和背压上限。远程连接关闭只 detach 代理 client，不能结束
tmux server/session 或 Pane 进程。

容器默认只映射 loopback。需要远程访问时，应使用可信反向代理终止 TLS，并保护
Admin Token。不要把数据库、A 配置、Bridge 日志或 tmux socket 上传为诊断附件。
