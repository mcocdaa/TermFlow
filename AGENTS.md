# TermFlow — Notes for Coding Agents

本文件面向在 TermFlow 仓库中工作的 AI Coding Agent。用户文档见 [README.md](README.md)，协议与操作手册详见 [docs/](docs/)。

## 1. Read First (必读指引)

1. 先阅读 [架构说明](docs/architecture.md) 与 [协议文档](docs/protocol.md)；运维与故障排查见 [docs/operations.md](docs/operations.md) 和 [docs/troubleshooting.md](docs/troubleshooting.md)。
2. TermFlow 架构分三层：
   - **Computer A (Node)**：运行 tmux、本地工作目录与守护进程，主动向上建立长连接。
   - **Control Plane B + Web C**：中继控制面 (FastAPI/ASGI) + 统一 Web 客户端。
   - **Agent Broker**：OpenCode 后端接入、人工审批流与事件重放通道。
3. 修改通信协议必须同步更新 `packages/protocol` 以及前端契约。

## 2. Repository Rules & Constraints (核心契约与安全规则)

- **工作区一致性**：Node.js 使用根目录 npm workspace，Python 使用 uv workspace (`packages/protocol`, `apps/control-plane`, `apps/node`)。
- **安全与凭据**：绝不提交 `TERMFLOW_ADMIN_TOKEN`、TOTP 密钥、SSH 私钥或用户终端会话持久化数据。
- **终端会话保活**：连接断开与客户端关闭绝不可导致 tmux 会话或后台进程异常终止。
- **多端发布规范**：Tauri 客户端、Web 客户端与后端容器镜像在 release 时统一受 tag 驱动。

## 3. Essential Commands (核心研发命令)

```bash
# 环境同步
uv sync --all-packages                 # 同步 Python workspace 依赖
npm install                            # 安装 npm workspace 依赖

# 代码检查与格式化
uv run ruff check .                    # 后端 Ruff 检查
uv run ruff format --check .           # 后端格式校验
uv run mypy packages/protocol apps/    # 强类型检查

# 测试套件
uv run pytest                          # 后端单元与协议测试
npm run typecheck                      # 客户端类型检查
npm run test                           # 前端单元测试

# 构建与验证
./scripts/verify.sh                    # 统一验证脚本
```

## 4. Verification Checklist (提交前自检)

- [ ] uv run pytest 与 npm test 全量通过
- [ ] Ruff 与 mypy 检查全绿无警告
- [ ] 涉及协议修改时已同步更新前后端契约定义
- [ ] 确保未跟踪本地 tmux 临时 socket 或会话日志
