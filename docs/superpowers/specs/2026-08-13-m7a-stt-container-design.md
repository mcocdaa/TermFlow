# M7a：可选 pinned STT 容器 profile + 第一个 Whisper provider 设计

> **已废弃（2026-08-16）：** 本文仅保留被撤销方案的历史记录，不属于
> TermFlow 0.2.0 实现或发布范围。现行边界见
> [`2026-08-16-m7-stt-scope-correction-design.md`](2026-08-16-m7-stt-scope-correction-design.md)：
> C 只向 B 提交普通文本，音频不跨越 C→B，B 不提供 STT 能力。

**日期：** 2026-08-13
**状态：** 设计 spec（M7 第二、三项，计划 §14/§16）
**范围：** B Control Plane、deploy compose、测试与验证
**工作树：** `feat/agent-broker-v0.2.0`

## 1. 目标

为 0.2.0 Agent Broker 交付 M7 剩余的"可选 STT 容器"两条：

1. 在 `deploy/compose.yaml` 增加一个**可选、pinned** 的 STT 容器 compose profile，默认不启动；启用后 B 通过内部网络访问它，不暴露宿主机端口。
2. 实现**第一个可选 STT provider**：B 进程内一个实现 `TranscriptionProvider` port 的 speaches（OpenAI 兼容 Whisper/faster-whisper 服务）HTTP 客户端，经 `TERMFLOW_STT_*` 配置接线，默认关闭（provider absence 时回退 `NullTranscriptionProvider`，fail-closed，503）。

`TranscriptDraft` 状态机与 raw-audio 删除契约保持 provider-independent：本任务只往既有管线喂 `TranscriptionResult`，不改 draft 模型、不改删除语义、不自动提交。

## 2. 非目标

- 移动端 press/hold 录音、上传态、draft 编辑/确认/取消 UI（M7 下一任务）。
- 已确认 draft 成为 `UserMessage` 的提交管线（后续 milestone）。
- GPU/CUDA 运行 profile（本 spec 只 ship CPU pinned 镜像；`-cuda` 镜像 tag 在 pin fixture 中记录为部署后续选项）。
- `available()` 实时健康探测语义（见 §5.3）。
- SSE 流式转录；只使用非流式 JSON 响应。
- Web `SpeechRecognition` 便捷适配器。
- 容器模型下载的专用 egress 网络（plan §16 `provider_egress` 部署后续；本 spec 用预置模型卷方案）。
- API key 轮换 UI/自动化。
- 每请求语言选择（port 签名不变）。

## 3. 背景与现状

- 计划 M7（`docs/superpowers/plans/2026-08-10-termflow-0.2.0-agent-broker.md` 1023-1031 行）已勾选 M7.1（port + upload API + `TranscriptDraft` 状态机）。未完成项：pinned STT 容器 profile；第一个可选 STT plugin（pinned Whisper 镜像或显式记录 provider 不可用）；移动端 UI；验证矩阵。
- 既有代码（`plugins/agent_broker/agent/transcription.py`）：`TranscriptionProvider` Protocol（`available()` / `transcribe(audio, *, mime_type) -> TranscriptionResult`）、`TranscriptionProviderError`、`NullTranscriptionProvider`。port 文档明确"B owns upload limits, raw-audio deletion, draft confirmation, provider disclosure, and no-auto-submit"。
- 既有 upload API（`api/transcription.py`）：10 MiB 大小上限（Content-Length + 流式双检）、webm/wav/mpeg magic 嗅探、WAV 采样率 8-96 kHz 与时长 ≤10 分钟解码前检查、解码后 `duration_seconds` 复核、并发 4、B 侧 60s 墙钟（`TRANSCRIPTION_TIMEOUT_SECONDS`）、draft TTL 1 小时、临时文件全路径 unlink、错误映射 413/415/422/502/503/504。`app.py` 组合根目前无条件装配 `NullTranscriptionProvider`。
- pin 调研结论（`tests/fixtures/opencode/opencode-pin.md` 记录 pin 方法学；`plugins/agent_broker/reuse.py` 记 §22 决定）：faster-whisper 维护停滞；参考容器为 **speaches-ai/speaches（MIT，活跃）**，`ghcr.io/speaches-ai/speaches:latest-cpu / latest-cuda`；原 fedirz/faster-whisper-server 已退役。
- 本 spec 通过 ghcr registry API 于 2026-08-13 捕获具体 pin：稳定版 tag **`0.8.3-cpu`**（digest `sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8`）与 **`0.8.3-cuda`**（digest `sha256:9abc6968a883e77ead9286c38a6ccf3139dcad518b783deeebb7be2ced50e972`）。镜像以非 root `ubuntu`（UID 1000）运行、`EXPOSE 8000`、内置 `DO_NOT_TRACK`/`DISABLE_TELEMETRY`/`HF_HUB_DISABLE_TELEMETRY`、镜像内装有 `curl` 与 `ffmpeg`（webm/wav/mpeg 均支持）。**M7 exit 实测修正：`/health` 并非公开端点**——不带 token 返回 403 `{"detail":"Not authenticated"}`，带 `Authorization: Bearer <API_KEY>` 返回 200；healthcheck 与相关文档按实测更新（pin fixture §1/§2、compose healthcheck、verify-stt.sh）。
- compose 安全基线（plan §16，`deploy/compose.yaml` opencode-agent 服务）：internal-only 网络、无 host 端口、`cap_drop: [ALL]`、`no-new-privileges`、`read_only` + tmpfs、固定非 root UID、CPU/内存/PID/ulimit 限制、healthcheck、日志轮转与脱敏、**pinned tag+digest（占位 digest 不可拉取，防未 pin 部署）**。
- 关键代码库约定：`httpx` 是 dev-group 依赖，运行时绝不 module-level import（`agent/opencode.py` 先例：注入 client 供测试、懒加载自建 client）。
- 约束：`agent_internal` 网络 `internal: true` **无外网 egress**；speaches 模型默认在请求时从 HuggingFace 动态下载（或 `PRELOAD_MODELS` 启动时下载），因此容器在 internal 网络上**必须预置模型卷**才能工作。
- compose 契约测试（`tests/deploy/test_compose_contract.py`）当前断言 services 恰好为 `["control-plane", "opencode-agent"]`——新增服务需同步更新。

## 4. 架构设计

### 4.1 整体数据流（不变式）

```text
C 上传受限音频
  -> B upload API（大小/格式/时长/并发/墙钟上限，全路径清理）
  -> SpeachesTranscriptionProvider.transcribe(audio, mime_type)   [新]
       POST {stt_url}/v1/audio/transcriptions (multipart, Bearer)
  -> TranscriptionResult（provider="speaches", model, transcript）
  -> TranscriptDraft（draft -> confirmed|cancelled|expired|consumed，仅 hash+元数据）
  -> C 确认/编辑后才可能成为 UserMessage（后续 milestone，永不自动提交）
```

### 4.2 compose profile：`stt-speaches`

新增服务 `stt-speaches`，**compose `profiles: ["stt"]`**，默认不启动。其强化基线对齐现有 opencode-agent 服务（plan §16）：

| 维度 | 值 |
|---|---|
| image | `ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8`（tag+digest 双 pin，绝不用 `latest`；实现时重捕获 digest，漂移则更新 pin fixture 并重新验证） |
| profiles | `["stt"]` |
| networks | `agent_internal` only（`internal: true`，无 egress，不挂 public A/C 网络） |
| ports | 无 host 端口；`expose: ["8000"]` |
| user | `"1000:1000"`（镜像默认 `ubuntu` UID 1000） |
| cap_drop / security_opt | `[ALL]` / `no-new-privileges:true` |
| read_only / tmpfs | `read_only: true`；`/tmp:size=64m,mode=1777,noexec,nosuid` |
| mem/cpus/pids/ulimits | `${STT_MEM_LIMIT:-2g}` / `${STT_CPUS:-2}` / 256 / nofile 1024 |
| volumes | `stt-models:/home/ubuntu/.cache/huggingface/hub`（模型缓存卷，名称 `${STT_MODELS_VOLUME:-stt-models}`；**必须预置，见 §4.4**） |
| environment | `UVICORN_PORT=8000`、`ENABLE_UI=false`（关闭 Gradio）、`LOG_LEVEL=warning`（压缩日志量）、`STT_MODEL_TTL=-1`（单模型常驻）、`PRELOAD_MODELS='["${STT_MODEL:-Systran/faster-distil-whisper-small.en}"]'`（**必须 JSON 数组形式**——speaches 的 `preload_models: list[str]` 走 pydantic-settings 复杂类型 env 的 JSON 解析，裸字符串会启动失败；启动即校验模型存在，缺失则退出——fail-loud）、`WHISPER__COMPUTE_TYPE=int8`、`API_KEY=${STT_API_KEY:?set STT_API_KEY}`（与 opencode 的 `:?` 必填部署机密一致；M7 exit 实测 `/health` 同样需要 Bearer token） |
| healthcheck | `curl -fsS -H "Authorization: Bearer $API_KEY" http://127.0.0.1:8000/health`（镜像内置 curl；M7 exit 实测 `/health` 需认证，无 token 403），interval 10s / timeout 3s / retries 3 / start_period 30s |
| restart / logging | `unless-stopped`；json-file 10m/5（与现有服务一致） |
| 迁移 | 无数据卷迁移；模型缓存卷为一次性内容，与 `termflow-data` 无关 |

`control-plane` 服务增加 env 透传：`TERMFLOW_STT_ENABLED=${TERMFLOW_STT_ENABLED:-false}`、`TERMFLOW_STT_URL=${TERMFLOW_STT_URL:-http://stt-speaches:8000}`、`TERMFLOW_STT_TOKEN=${STT_API_KEY:-}`、`TERMFLOW_STT_MODEL=${STT_MODEL:-Systran/faster-distil-whisper-small.en}`、`TERMFLOW_STT_TIMEOUT_SECONDS=${STT_TIMEOUT_SECONDS:-55}`。

**启用契约（双重显式 opt-in）**：`docker compose --profile stt up -d` 且设置 `TERMFLOW_STT_ENABLED=true` 与 `STT_API_KEY`；否则 STT 服务不启动、B 侧 `NullTranscriptionProvider`（503）。compose 不支持按 profile 条件注入 env，因此文档（`.env.example`、README、pin fixture）必须把"启用 = profile + env 两个动作"写清楚。

### 4.3 provider 实现：`plugins/agent_broker/agent/speaches.py`

新模块 `SpeachesTranscriptionProvider`，实现 `TranscriptionProvider` Protocol：

```python
SpeachesTranscriptionProvider(
    base_url: str,                 # 例如 http://stt-speaches:8000（来自 TERMFLOW_STT_URL）
    *,
    model: str,                    # 来自 TERMFLOW_STT_MODEL
    token: str | None = None,      # Bearer；来自 TERMFLOW_STT_TOKEN
    timeout_seconds: float = 55.0, # 必须 < B 侧 60s（见 §5.1）
    max_audio_bytes: int = 10 MiB, # 防御性复核，与 B 上限一致
    client: object | None = None,  # 测试注入（httpx.MockTransport 先例）；None 时懒加载 httpx
)
```

**API 形状**（OpenAI 兼容，speaches 文档与 `POST /v1/audio/transcriptions` 一致）：

- `POST {base_url}/v1/audio/transcriptions`，`multipart/form-data`：
  - `file=(<safe_filename>, audio, mime_type)`，safe_filename 由 mime 映射：`audio/wav→speech.wav`、`audio/webm→speech.webm`、`audio/mpeg→speech.mp3`；其他 mime 直接 `TranscriptionProviderError`（port 独立，不信任调用方）。
  - `model=<config model>`、`response_format=json`（显式，稳定解析）。
  - 头：`Authorization: Bearer <token>`（仅当 token 配置时）。
  - 请求超时 = `timeout_seconds`。
- 成功（2xx）：解析 JSON `{"text": ...}`；构造 `TranscriptionResult(transcript=text, provider="speaches", region=None, language=None, duration_seconds=None, model=<config model>)`；pydantic 校验（transcript 1..4096 等）失败时把 `ValidationError` 包装为 `TranscriptionProviderError`（port 契约：provider 只抛 `TranscriptionProviderError`）。`duration_seconds` 由 B 侧按同一上限复核（`_transcribe_with_bounds`）。
- 失败映射（全部 → `TranscriptionProviderError`，日志只含状态码/稳定类别，**绝不记录 raw audio、请求体、响应体**）：
  - 连接失败/拒绝（`ConnectError` 类）→ "speaches unreachable"；
  - 请求超时（`TimeoutException` 类）→ "speaches timed out"；
  - 非 2xx（401/403/413/422/500/503…）→ "speaches returned HTTP <status>"；
  - 非 JSON / 缺 `text` / 空 text / 超界 text → "invalid transcription response"。
- 防御性复核（B 已先于 provider 强制执行，这里是 port 边界的纵深防御）：`0 < len(audio) <= max_audio_bytes` 否则错误；mime 白名单如上。
- **httpx 懒加载**：与 `agent/opencode.py` 完全一致的约定——`httpx` 是 dev-group 依赖，provider 接受注入 client（duck-typed `post`），未注入时在方法内 `import httpx` 自建 `httpx.AsyncClient(timeout=timeout_seconds)`；模块级绝不 import httpx。不新增运行时依赖。
- `available() -> bool`：**配置派生**（构造成功即为 True），不做实时探测（决策见 §5.3）。
- 模块 docstring 必须声明：raw audio 不落日志、不持久化、不留存（port 契约 §14）。

### 4.4 模型卷预置契约（internal 网络无 egress 的解法）

`agent_internal` 是 `internal: true`，speaches 无法自行从 HuggingFace 下载模型。契约：

1. 首次启用前，操作者必须**一次性预置 `stt-models` 卷**：用同一 pinned 镜像在默认（有 egress）网络上执行 `huggingface_hub.snapshot_download('Systran/faster-distil-whisper-small.en')` 并把该卷挂到 `/home/ubuntu/.cache/huggingface/hub`。命令形状（精确形式在实现时由容器集成测试固化并写入 pin fixture 与 README）：

   ```bash
   docker run --rm \
     -v stt-models:/home/ubuntu/.cache/huggingface/hub \
     ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8 \
     python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-distil-whisper-small.en')"
   ```

   镜像 venv 自带 `python` 与 `huggingface_hub`（speaches 的模型下载依赖）；新命名卷首次挂载时从镜像的 `/home/ubuntu/.cache/huggingface/hub` 继承属主（`ubuntu` UID 1000，镜像已预建该目录），容器默认 user 1000 即可写入。
2. 运行时 `PRELOAD_MODELS` 指向同一模型：模型已在卷内则直接加载；缺失则容器启动失败退出（fail-loud，不逐请求失败）。
3. 换模型（`STT_MODEL`）需要重新预置卷。CUDA 镜像、`provider_egress` 允许列表网络、按需下载是部署后续选项（记录于 pin fixture，不在本 spec 实现）。

### 4.5 配置（B 侧，`config.py`）

新增字段（`env_prefix="TERMFLOW_"`，沿用 `agent_mcp_*` 模式，均**默认关闭**）：

| 字段 | env | 默认 | 校验 |
|---|---|---|---|
| `stt_enabled: bool` | `TERMFLOW_STT_ENABLED` | `False` | — |
| `stt_url: str | None` | `TERMFLOW_STT_URL` | `None` | 必须为 http(s) URL，无 userinfo/query/fragment（复用 `_web_origin` 类校验的宿主 + scheme 规则） |
| `stt_token: SecretStr | None` | `TERMFLOW_STT_TOKEN` | `None` | SecretStr（不落日志）；**空串归一化为 `None`**（compose 未启用 profile 时 `${STT_API_KEY:-}` 会传空串，不能误判为已配置） |
| `stt_model: str` | `TERMFLOW_STT_MODEL` | `"Systran/faster-distil-whisper-small.en"` | 非空 |
| `stt_timeout_seconds: float` | `TERMFLOW_STT_TIMEOUT_SECONDS` | `55.0` | `1.0 <= x <= 59.0`（必须严格低于 B 侧 60s 墙钟，防误配导致 502/504 竞态；注释注明与 `api/transcription.py` 的耦合） |

组合校验（`model_validator`）：`stt_enabled=True` 且 `stt_url is None` → 配置错误（fail at startup，不静默回退）。

### 4.6 装配（`app.py`）

替换 `app.state.transcription_provider = NullTranscriptionProvider()` 为：

```python
if settings.stt_enabled:
    app.state.transcription_provider = SpeachesTranscriptionProvider(
        settings.stt_url, model=settings.stt_model,
        token=settings.stt_token.get_secret_value() if settings.stt_token else None,
        timeout_seconds=settings.stt_timeout_seconds,
    )
else:
    app.state.transcription_provider = NullTranscriptionProvider()
```

`transcription_semaphore` / `transcription_timeout_seconds`（60s 墙钟）/ `transcription_staging_dir` 不变。upload API 无改动（`get_transcription_provider` 已按 `app.state` 读取）。

### 4.7 TranscriptDraft 不变式确认（M7 第三项）

`persistence/models.py` 的 `TranscriptDraft`（hash + 元数据，`provider` 列存 `"speaches"` 字符串）与删除契约**零改动**：provider 只生产 `TranscriptionResult`，draft 生命周期/所有权/一次性确认/1 小时 TTL/raw-audio 删除全部保持在 B。`region` 留空、`language` 留空（port 签名不带语言参数，模型自检语言或英语专用模型）。

### 4.8 pin fixture：`tests/fixtures/speaches/speaches-pin.md`

仿照 `tests/fixtures/opencode/opencode-pin.md` 结构记录 pinned 契约：镜像 tag+digest（CPU/CUDA 两个变体）、`/v1/audio/transcriptions` 端点契约（multipart 字段、默认 JSON `{"text"}`、response_format=json）、`/health`、Bearer 鉴权（`API_KEY`）、环境配置表（`PRELOAD_MODELS`/`ENABLE_UI`/`LOG_LEVEL`/`STT_MODEL_TTL`/`WHISPER__COMPUTE_TYPE`）、模型 pin（`Systran/faster-distil-whisper-small.en`，英语专用 distil 小模型，CPU 友好）、许可证 MIT、遥测禁用默认值、内部网络无 egress 的预置要求、验证状态（live 容器验证在 M7 exit / M8，之前标 `unknown`——沿用 opencode capability_matrix 的诚实标记约定）。末尾嵌入机器可读 `termflow-speaches-pin-contract` JSON 块。

## 5. 关键设计决策与理由

### 5.1 超时/大小/格式限制的层级（M7 验证矩阵问题）

- **权威层 = B upload API**（已实现）：10 MiB 大小、格式 sniff、WAV 采样率/时长、解码后时长复核、并发 4、60s 墙钟（504）。`TranscriptionResult` 的 4096 字符上限与 `duration_seconds` 复核同样在 B。
- **provider 层 = 纵深防御**：重复 `0 < len(audio) <= 10 MiB` 与 mime 白名单（port 可独立被未来插件调用，不信任入参）；响应文本长度由 `TranscriptionResult` 校验兜底。
- **超时两层**：provider HTTP 超时（默认 55s）< B 侧墙钟 60s；provider 超时 → `TranscriptionProviderError` → 502；B 侧墙钟先到 → 504。config 校验（≤59s）保证不会出现"provider 无响应但 B 先杀"的竞态反转。

### 5.2 错误语义（fail-closed 三态）

| 场景 | 结果 | 码 |
|---|---|---|
| 未配置/未启用（NullProvider，`available()=False`） | 上传前 503，不消费 body | `speech_to_text_unavailable` |
| 已配置但容器不可达/出错/响应非法 | `TranscriptionProviderError` | 502 `transcription_failed` |
| 超过 B 侧 60s 墙钟 | `TimeoutError` | 504 `transcription_timeout` |

### 5.3 `available()` 不做实时探测

候选：构造时或每次上传前 GET `/health`。否决理由：每次上传增加一次网络往返与新的失败面；`/health` 通过不代表模型已加载，探测值本身不可靠；配置派生语义与 502/503 三态已足够清晰，容器存活性由 compose healthcheck 暴露。记录为决策，后续若需要"配置了但容器不在线 → UI 显示不可用"，可加带负缓存 TTL 的探测（YAGNI，不在本 spec）。

### 5.4 镜像 pin 方式

计划 §16 要求"exact tested version and image digest; never use latest"。本 spec 直接 pin 已捕获的具体 tag+digest（比 opencode 的不可拉取占位更强，因为 M7a 就运行容器集成测试）；实现时重捕获 digest 并写入 pin fixture + compose 契约测试断言（tag+digest 双 pin，`latest*` 标签在契约测试中被禁止）。

### 5.5 默认模型选择

`Systran/faster-distil-whisper-small.en`：英语专用 distil 小模型，CPU int8 下延迟与内存友好（默认 compose 为 CPU 部署）；多语言部署可经 `STT_MODEL` 覆盖。模型 id 只存在于 B 配置与 provider 层（`TranscriptionResult.model` 披露字段），不进入 `packages/protocol` 领域模型、不持久化进 draft（draft 只存 provider/region 字符串），符合 §22.1"provider types 不进 protocol/domain"。

## 6. 文件变更

**新增：**

| 文件 | 内容 |
|---|---|
| `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/agent/speaches.py` | `SpeachesTranscriptionProvider`（§4.3） |
| `apps/control-plane/tests/fixtures/speaches/speaches-pin.md` | pinned 容器契约 fixture（§4.8） |
| `apps/control-plane/tests/test_speaches_provider.py` | provider 单元测试（httpx.MockTransport，§7.1） |
| `apps/control-plane/tests/test_stt_config.py` | 配置校验测试（§7.2） |
| `scripts/verify-stt.sh` | Docker-gated 容器集成验证脚本（§7.3），并挂入 `scripts/verify.sh` 的 Docker 门段 |

**修改：**

| 文件 | 内容 |
|---|---|
| `apps/control-plane/src/termflow_control_plane/config.py` | §4.5 五个 `stt_*` 字段 + 组合校验 |
| `apps/control-plane/src/termflow_control_plane/app.py` | §4.6 装配（含 import） |
| `deploy/compose.yaml` | `stt-speaches` 服务 + `profiles` + `stt-models` 卷 + control-plane env 透传 |
| `.env.example` | `TERMFLOW_STT_*`、`STT_API_KEY`/`STT_MODEL`/`STT_MEM_LIMIT`/`STT_CPUS`/`STT_MODELS_VOLUME`/`STT_TIMEOUT_SECONDS` 文档与启用契约 |
| `README.md` | STT 启用步骤（profile + env + 模型卷预置），披露 provider/本地性/模型下载要求（plan §14 要求"visible in deployment settings"） |
| `tests/deploy/test_compose_contract.py` | services 列表断言更新 + `stt-speaches` 强化断言 + digest/latest 禁令（§7.4） |
| `apps/control-plane/tests/test_transcription_api.py` | 装配级测试：`stt_enabled=True` 接线 Speaches、默认仍 503（§7.5） |
| `apps/control-plane/src/termflow_control_plane/plugins/agent_broker/reuse.py` | §22 决定 notes 追加 pinned tag/digest 引用（traceability；`test_reuse.py` 断言不改） |

**不改：** `api/transcription.py`、`persistence/models.py`（`TranscriptDraft`）、迁移、client-contracts。

## 7. 测试策略

### 7.1 provider 单元测试（`test_speaches_provider.py`，无 Docker）

用 `httpx.MockTransport` 注入（dev-group httpx，先例同 opencode 测试）：

- 成功路径：断言 multipart 含 `file`/`model`/`response_format=json`、mime→文件名映射、`Authorization: Bearer` 存在与否、`TranscriptionResult`（provider="speaches"、model 透传、duration_seconds=None）。
- 失败路径：连接错误、HTTP 非 2xx（401/422/500/503）、超时、非 JSON、缺 `text`、空 text、>4096 字符 text → 全部 `TranscriptionProviderError`；断言日志无 audio/响应体内容。
- 防御边界：空 audio、>10 MiB audio、未知 mime → `TranscriptionProviderError`。
- `available()` 恒 True（配置派生）；token 为空时不发送 Authorization 头。

### 7.2 配置测试（`test_stt_config.py`）

- 默认值：`stt_enabled=False`、`stt_url=None` → NullProvider 路径。
- `stt_enabled=True` 无 URL → 启动期配置错误。
- URL 含 userinfo/query → 拒绝；`stt_timeout_seconds` 0.5 / 60 越界 → 拒绝；`stt_token` 为 SecretStr 且不序列化进日志。

### 7.3 容器集成测试（`scripts/verify-stt.sh`，**Docker-gated，M7 exit 需要 Docker**）

Docker 可用才运行；不可用时**显式跳过并记录为 unverified**（M8 契约：unavailable 环境不推断为通过）：

- 按 pin digest 拉取镜像，校验 digest 与 pin fixture 一致（拉取失败即失败——占位/漂移 fail-loud）。
- 以 compose 相同的强化参数（internal 网络、无端口发布、`cap_drop ALL`、`no-new-privileges`、`read_only`+tmpfs、user 1000:1000、卷挂载）启动一次性容器；`docker inspect` 校验 CapEff、非 root（`stat -c %u /proc/1`）、只读 rootfs、健康检查通过。
- 端到端：B（compose `--profile stt`）真实 WAV 上传 → 201 draft；无 profile 部署 → 503。
- 模型卷预置命令的首次执行即被此脚本固化。

### 7.4 compose 契约测试（更新 `test_compose_contract.py`）

- `test_compose_is_single_worker_and_persists_only_metadata` 的 services 断言改为三服务；`stt-models` 卷断言。
- 新 `test_stt_speaches_service_is_optional_and_hardened`：`profiles == ["stt"]`、networks 仅 `agent_internal`、无 `ports`、`cap_drop == ["ALL"]`、`read_only is True`、无 docker.sock、`user == "1000:1000"`、`ENABLE_UI=false`、`LOG_LEVEL=warning`、`API_KEY` 用 `:?` 必填、`PRELOAD_MODELS` 存在、healthcheck 以 `/health` 结尾、mem/cpus 有 env 默认。
- 新 `test_stt_image_is_pinned_tag_and_digest`：image 值含 `@sha256:` 且 tag 不以 `latest` 开头（全文扫描 `latest-` 标签禁用）。
- control-plane env 断言：`TERMFLOW_STT_ENABLED` 默认 `false`。

### 7.5 装配级测试（`test_transcription_api.py` 增量）

- 默认 settings → `app.state.transcription_provider` 为 `NullTranscriptionProvider`，上传 503（既有测试保持）。
- `stt_enabled=True` + url/token → provider 为 `SpeachesTranscriptionProvider`；上传经注入 client 走真实 provider 路径（复用 MockTransport）→ 201。

### 7.6 M7 验证矩阵逐项映射（计划 1029 行）

| 矩阵项 | 层 | 已有/新增 |
|---|---|---|
| permission denial（401/403） | B upload API | 已有 `test_upload_requires_auth_binding_and_conversation`、owner-only 测试 |
| format/size/duration limits | B upload API（权威）+ provider（纵深） | 已有 413/415/422 测试；新增 provider 防御边界测试 |
| timeout | B 墙钟 504（已有）+ provider HTTP 超时 502（新增） | `test_provider_timeout_returns_504_and_cleans_temp` 已有；新增 `test_speaches_provider.py` 超时用例 |
| provider absence | 装配层 | 已有 503 测试；新增装配断言（默认 NullProvider） |
| raw-audio deletion | B upload API | 已有（临时文件 unlink + 无泄漏测试）；provider 测试断言不落日志 |
| transcript-not-auto-submit | B draft 状态机 | 已有 confirm/cancel/expiry/一次性 CAS 测试；draft→UserMessage 属后续 milestone，本 spec 无新增 |

## 8. 风险与缓解

1. **只读 rootfs 破坏 speaches 运行时写路径**（镜像未承诺只读兼容）。缓解：容器集成测试（§7.3）以最终 compose 参数启动并跑真实转录；若失败，回退方案为去掉 `read_only` 保留其余全部强化，并在 pin fixture 记录该偏差（不静默放宽）。
2. **模型下载依赖 egress，而 `agent_internal` 无 egress**。缓解：预置模型卷契约（§4.4）+ `PRELOAD_MODELS` 启动期 fail-loud（缺失即退出而非每请求失败）；预置命令由集成测试固化；文档显式披露（plan §14"model download requirements visible"）。
3. **上游镜像 tag/digest 漂移**（speaches 活跃发布）。缓解：compose 契约测试禁止 `latest*` 并要求 `@sha256:`；pin fixture 记录捕获日期与变体；实现时与 M8 各重捕获一次 digest，漂移则更新 fixture 并重跑集成测试。
4. **httpx 仅 dev-group，生产镜像无 httpx**。缓解：沿用 opencode.py 先例——注入 client + 懒加载；模块级零 httpx import；装配代码只 import provider 类本身。
5. **超时层级竞态**（provider 超时 ≥ B 墙钟会反转错误码语义）。缓解：config 校验 `1..59` 硬约束 + 文档注释耦合说明。
6. **speaches API 契约漂移**（OpenAI 兼容面变更）。缓解：pin fixture 冻结端点/字段/响应形状；容器集成测试在 M7 exit 实测；漂移即更新 fixture 与 provider 映射（§22.1 direct-adoption 契约）。

## 9. 决策摘要

- **容器**：`ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8`（CPU，tag+digest 双 pin；`0.8.3-cuda` 记录为部署后续）；MIT；非 root UID 1000。
- **profile**：`stt` profile 内服务 `stt-speaches`，仅挂 `agent_internal`，无 host 端口，强化参数对齐 opencode-agent。
- **启用契约**：`--profile stt` + `TERMFLOW_STT_ENABLED=true` + `STT_API_KEY`（`:?` 必填）三重显式动作；默认全部关闭。
- **provider**：B 内 `SpeachesTranscriptionProvider`，OpenAI 兼容 `POST /v1/audio/transcriptions`（multipart `file`/`model`/`response_format=json`，可选 Bearer）；错误全部映射 `TranscriptionProviderError`；httpx 懒加载 + 注入 client（opencode.py 先例），无新运行时依赖。
- **限制层级**：B upload API 为权威（大小/格式/时长/并发/60s 墙钟）；provider 仅防御性复核（大小/mime）；provider 超时 55s < B 60s，config 校验 ≤59s。
- **错误三态**：未配置→503；已配置但失败→502；B 墙钟超时→504。
- **`available()`**：配置派生，无实时探测。
- **模型**：`Systran/faster-distil-whisper-small.en`（可经 `STT_MODEL` 覆盖）；`agent_internal` 无 egress → 预置 `stt-models` 卷 + `PRELOAD_MODELS` fail-loud。
- **TranscriptDraft**：零改动，provider-independent 确认（仅 `provider="speaches"` 进披露元数据）。
- **验证**：provider/配置/compose 契约/装配为无 Docker 测试；`scripts/verify-stt.sh` 为 Docker-gated 容器集成（M7 exit），不可用环境显式 unverified。
