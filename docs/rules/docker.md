---
title: Docker 部署规范
description: 通用Docker容器化部署规范
keywords: [docker, compose, 部署规范]
version: "3.0"
---

# Docker 部署规范

本规范适用于任何软件项目的 Docker 容器化部署。

## 1. 默认形态：单镜像全栈

前后端项目默认打成**一个镜像**：多阶段构建前端静态文件，COPY 进后端镜像，由后端在**同一端口**提供页面与 API。

优点：一条 `docker run` 即可部署，无跨容器网络与 CORS 配置；多服务（DB/缓存/MQ）场景才需要拆分。

### 1.1 目录结构

```
project-root/
├── docker-compose.yml     # 唯一入口（单服务，同时声明 image 与 build）
├── .env.example           # 配置模板（提交到 Git）
├── .dockerignore
├── backend/
│   └── Dockerfile         # 多阶段：frontend-builder + 运行镜像
└── scripts/
    ├── start.sh           # 统一启动入口
    └── stop.sh
```

**规则：**

- 单服务不预先做 compose 分层；出现第二个服务时再拆分或引入 `compose.override.yml`
- 不再为"仅前端"维护独立镜像：页面随全栈镜像发布，前端开发走本地 dev server

### 1.2 多阶段 Dockerfile 示例

```dockerfile
# 构建前端静态文件
FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build:web

# 运行镜像
FROM python:3.12-slim
ENV SERVE_STATIC_FILES=true STATIC_FILES_DIR=/app/static
WORKDIR /app/backend
COPY backend/pyproject.toml backend/poetry.lock ./
RUN pip install --no-cache-dir poetry==2.3.3 \
    && poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi --no-root
COPY backend/ ./
COPY --from=frontend-builder /build/frontend/dist /app/static
HEALTHCHECK CMD curl -fsS http://localhost:3000/health || exit 1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 3000"]
```

### 1.3 Compose 示例

```yaml
services:
  app:
    image: ghcr.io/<owner>/<repo>:latest
    build:
      context: .
      dockerfile: backend/Dockerfile
    restart: unless-stopped
    ports:
      - "${BACKEND_EXTERNAL_PORT:-3001}:3000"
    environment:
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - API_KEY=${API_KEY:-}
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:3000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

## 2. 配置管理

- `.env.example` 提交到 Git，`.env` 不入库；`docker compose` 会自动读取 `.env` 做变量替换
- 端口、日志级别、开关等非敏感项走 `.env`
- API Key / Token / Cookie 默认同样通过 `.env` 注入（文件已 gitignore）；需要更强隔离时再使用 Docker secrets 或外部密钥系统
- 应用统一从环境变量读取配置，可兼容 `env:VAR` 形式的引用解析
- `.env.example` 保持最小集：只保留真实被读取的键

## 3. 运行约定

- 镜像内部端口固定（如 3000），宿主机端口由 `*_EXTERNAL_PORT` 控制
- 必须定义 `HEALTHCHECK`，并与 compose 的 healthcheck 保持一致
- 基础镜像使用具体版本标签（禁止 `FROM ...:latest`）
- apt 安装使用 `--no-install-recommends` 并清理 `/var/lib/apt/lists/*`
- 应用需要写挂载目录时再引入非 root 用户，并同步处理目录权限

## 4. 启动脚本

所有启动操作统一走 `scripts/`，README 中的命令必须与脚本行为一致：

| 命令 | 说明 |
|------|------|
| `./scripts/start.sh` | 构建并启动 Docker 全栈（默认） |
| `./scripts/start.sh local [all\|backend\|frontend]` | 本地开发进程（不经 Docker） |
| `./scripts/stop.sh` | 停止 Docker 全栈 |

脚本负责：首次运行时从 `.env.example` 生成 `.env`、加载变量、启动服务并打印访问地址。

## 5. 发布（CD）

- 打 `v*` tag 时由 CI 构建并推送镜像到 GHCR：`ghcr.io/<owner>/<repo>`
- 镜像 tag 同时产出语义化版本（`1.2.3`）与 `latest`
- 部署端可 `docker compose pull && docker compose up -d` 升级

## 6. 故障排查

| 问题 | 解决方案 |
|------|---------|
| 端口被占用 | 修改 `.env` 中 `*_EXTERNAL_PORT` |
| 页面 404 | 确认构建产物已 COPY 到 `STATIC_FILES_DIR`，且 `SERVE_STATIC_FILES=true` |
| 服务启动失败 | 查看日志 `docker compose logs -f` |
| 配置未生效 | `docker compose config` 确认变量替换结果 |
