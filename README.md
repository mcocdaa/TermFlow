# TermFlow

TermFlow 让你通过浏览器、手机或原生客户端远程使用另一台电脑上的 tmux 终端。

```text
手机浏览器 / 桌面浏览器 / Windows、Linux、macOS、Android、iOS Simulator 客户端
                                  ↓ HTTPS / WSS
                         Control Plane B + Web C
                                  ↑ A 主动连接
                    Computer A（tmux、命令和工作目录）
```

关闭客户端或短暂断网不会停止 A 上运行的命令。v0.1.0 管理
`Computer → Term → Window → Pane`；Agent 和语音能力不属于这个版本。

## 安装 B + Web C

B 和 Web C 在同一个 Docker 镜像中。下面的命令使用本地目录保存数据库与 TOTP 主密钥，
并且只在宿主机 `127.0.0.1:8000` 监听：

```bash
mkdir -p data totp-secrets
docker network create --internal termflow-net

docker run -d \
  --name termflow-control-plane \
  --restart unless-stopped \
  --network termflow-net \
  --publish 127.0.0.1:8000:8000 \
  --volume "$PWD/data:/app/data" \
  --volume "$PWD/totp-secrets:/app/totp-secrets" \
  --env TERMFLOW_ADMIN_TOKEN='<至少 32 字节的管理员 token>' \
  --env TERMFLOW_DATABASE_URL=sqlite+aiosqlite:////app/data/termflow.db \
  --env TERMFLOW_PUBLIC_BASE_URL=https://termflow.example.com \
  --env TERMFLOW_ALLOW_INSECURE_LOOPBACK=false \
  --env TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE=/app/totp-secrets/totp-master-key \
  ghcr.io/mcocdaa/termflow-control-plane:v0.1.0
```

检查服务：

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

公网部署时，将 HTTPS/WSS 反向代理指向 `127.0.0.1:8000`，并把
`TERMFLOW_PUBLIC_BASE_URL` 设置为用户实际访问的 HTTPS origin。B 启动后，在 Web C 的
Computers 页面生成 Computer A 使用的一次性注册码。

## 安装 Computer A

### Linux

Linux x86_64 需要 tmux 3.2 或更高版本：

```bash
curl -fsSL \
  https://github.com/mcocdaa/TermFlow/releases/download/v0.1.0/install-termflow-node.sh \
  | bash

termflow login \
  --server https://termflow.example.com \
  --code '<Web C 生成的一次性注册码>'

termflow new --name project-a
```

使用 tmux 默认的 `Ctrl+B`、再按 `D` 可退出当前界面，Term 会继续运行。重新进入：

```bash
termflow attach project-a
```

### Docker

Docker A 是一个后台常驻的计算节点。身份和工作目录保存在本地目录，A 不开放端口，只通过
`termflow-net` 连接 B：

```bash
mkdir -p termflow-node-identity termflow-node-work

docker run -d \
  --name termflow-node \
  --restart unless-stopped \
  --network termflow-net \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add SETUID \
  --cap-add SETGID \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp \
  --volume "$PWD/termflow-node-identity:/home/termflow" \
  --volume "$PWD/termflow-node-work:/work" \
  --env TERMFLOW_SERVER=http://termflow-control-plane:8000 \
  --env TERMFLOW_CODE='<Web C 生成的一次性注册码>' \
  --env TERMFLOW_ALLOW_INSECURE_HTTP=true \
  --env TERMFLOW_NEW=demo \
  ghcr.io/mcocdaa/termflow-node:v0.1.0
```

Web C 进入 Docker A 的 Term 默认使用 Bash。如需改用 POSIX sh，在上述
`docker run` 命令中追加：

```bash
--env TERMFLOW_SHELL=sh
```

`TERMFLOW_SHELL` 只接受 `bash` 和 `sh`。修改后需要保留身份和工作目录，并
重新创建 Docker A 容器；新的 tmux pane 使用所选 shell，已经运行的 pane 不会热切换。

进入 Docker A 的 Term：

```bash
docker exec --user termflow -it termflow-node termflow attach demo
```

查看服务状态：

```bash
docker logs termflow-node
```

`termflow-node-identity/` 保存 A 的身份，`termflow-node-work/` 保存用户文件。这两个目录由 A
管理；删除目录会删除对应数据。

## 可选：语音转录（STT，Agent Broker 0.2.0）

STT 是可选部署，默认关闭：语音上传返回 503 `speech_to_text_unavailable`，
不影响文本 Agent 功能。启用后音频由**部署本地**的 speaches 容器转录
（OpenAI 兼容 Whisper 服务，MIT），音频只经过 B → 内部网络 `agent_internal`
上的 STT 容器，不发送到任何外部服务；转录结果以 1 小时 TTL 的 draft 返回
客户端确认，B 从不自动提交、从不持久化原始音频。模型文件由操作者在首次
启用前下载到本地卷（见下），因为内部网络没有外网出口。

启用需要三个显式动作（全部满足才生效）：

1. 设置机密与环境：在 `.env` 中设置 `STT_API_KEY`（必填，compose 校验；
   同时是 B 侧 `TERMFLOW_STT_TOKEN` 的来源），并设置
   `TERMFLOW_STT_ENABLED=true`；
2. 预置模型卷（一次性）：模型缓存卷必须先在**有外网**的网络上用同一
   pinned 镜像预置，`agent_internal` 无外网出口：

   ```bash
   docker run --rm \
     -v stt-models:/home/ubuntu/.cache/huggingface/hub \
     ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8 \
     python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-distil-whisper-small.en')"
   ```

3. 带 profile 启动：`docker compose --profile stt up -d`。

默认模型 `Systran/faster-distil-whisper-small.en`（英语专用 distil 小模型，
CPU 友好）；多语言部署可通过 `STT_MODEL` 覆盖，换模型必须重新预置卷。
镜像 tag+digest 双 pin（`0.8.3-cpu`，禁止 `latest`）；STT 容器契约详见
`apps/control-plane/tests/fixtures/speaches/speaches-pin.md`。

## 客户端

正式构建版本的解析顺序固定为
`Git Tag > TERMFLOW_BUILD_VERSION > 0.2.0-dev.0`。Tag Release 直接使用 `vX.Y.Z`
（以及受支持的 prerelease/build metadata）中的版本；手动 workflow 可填写可选版本，本地构建可设置
`TERMFLOW_BUILD_VERSION=1.2.3`。两者都没有时使用明确的开发版本
`0.2.0-dev.0`，不会被误认为正式 Release。Web C 已包含在 Control Plane 镜像中。Windows、Linux、macOS、Android 和 iOS Simulator
客户端从 [GitHub Releases](https://github.com/mcocdaa/TermFlow/releases) 下载。

### 移动端录音权限

按住说话的语音输入需要系统麦克风权限：

- **Android**：`gen/android/` 由 `tauri android init` 每次全新生成且不被 git 跟踪，
  录音所需 `RECORD_AUDIO` / `MODIFY_AUDIO_SETTINGS` 权限由补丁脚本幂等注入。本地开发在
  `npm run tauri --workspace @termflow/tauri-client -- android init` 之后运行一次：

  ```bash
  python3 scripts/patch-mobile-manifests.py
  ```

  脚本失败（如 manifest 缺失）会以非零退出码阻断构建；CI 在 `android init --ci`
  之后自动执行同一脚本。
- **iOS**：`tauri.ios.conf.json` 已配置 `bundle.iOS.infoPlist = Info.ios.plist`
  （最低系统版本 15.0），构建时自动合并 `NSMicrophoneUsageDescription`
  「用于按住说话并将语音转写为文字，录音仅上传到您连接的 TermFlow 服务器」，
  无需手动步骤；CI 在 iOS 构建后断言产物 Info.plist 包含该键。

## 更新与备份

使用新的精确 tag 重建容器即可升级。升级或回退前备份 B 的 `data/`、`totp-secrets/`，
以及 A 使用的 Docker volumes。不要使用 `latest`，也不要在升级时删除数据卷。

详细说明见 [部署与恢复](docs/operations.md)。

## 技术架构

| 组件 | 技术 | 职责 |
| --- | --- | --- |
| Computer A | Python 3.12、tmux、asyncio、WebSocket | 运行 Term、管理 tmux，并主动连接 B |
| Control Plane B | FastAPI、Uvicorn、SQLAlchemy 2、Alembic、SQLite | 认证、Computer 注册、状态同步和终端流量转发 |
| Web C | Vue 3、TypeScript、Vite、xterm.js | 浏览器和手机端的终端与管理界面 |
| 原生 C | Tauri 2、Rust，共用 Vue UI | Windows、Linux、macOS、Android 和 iOS Simulator 客户端 |
| Protocol | Pydantic 2 | A、B 之间的版本化消息协议 |

A 保存并执行真实终端进程；B 不执行 A 的 shell 命令，只负责认证、路由和转发。Web C
由 B 的同一镜像提供，原生客户端复用同一套客户端核心、UI 和协议契约。

## 开发与文档

从源码构建、开发环境和 Release 流程不放在安装入口中：

- [部署与恢复](docs/operations.md)
- [架构与进程边界](docs/architecture.md)
- [协议](docs/protocol.md)
- [API 示例](docs/api-examples.md)
- [Web C](docs/web-client.md)
- [安全与隐私](docs/security.md)
- [排障](docs/troubleshooting.md)
- [构建与发布](docs/github-actions.md)
- [设计与实施记录](docs/superpowers/README.md)
