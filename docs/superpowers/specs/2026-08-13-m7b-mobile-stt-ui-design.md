# M7b：移动端 press/hold 录音 + 转写 draft UI 设计

> **已废弃（2026-08-16）：** 本文仅保留被撤销方案的历史记录，不属于
> TermFlow 0.2.0 实现或发布范围。现行边界见
> [`2026-08-16-m7-stt-scope-correction-design.md`](2026-08-16-m7-stt-scope-correction-design.md)：
> C 只向 B 提交普通文本，音频不跨越 C→B，B 不提供 STT 能力。

**日期：** 2026-08-13
**状态：** 设计 spec（M7 第四、五项，计划 §14）
**范围：** Tauri 移动客户端（Android/iOS）、client-core、client-ui、最小 B 侧能力暴露
**工作树：** `feat/agent-broker-v0.2.0`

## 1. 目标

为 0.2.0 Agent Broker 交付计划 M7（`docs/superpowers/plans/2026-08-10-termflow-0.2.0-agent-broker.md` 1023-1031 行）剩余的两条客户端项：

1. 移动端 press/hold 录音、上传态、转写 draft 展示/编辑/确认/取消、以及 fallback UI（1028 行）。
2. 以客户端侧测试逐项验证权限拒绝、格式/大小/时长限制、超时、provider 缺失、raw-audio 删除（客户端视角）、转写不自动提交（1029 行）。

录音发生在移动端 WebView（`getUserMedia`），音频经 **Rust 持有** 的 multipart 上传命令提交到既有 `/api/v1/agent/transcription/drafts`；返回的 transcript 只在用户**显式确认**后，经 M4.5 submit 端点以 `draft_ref` 挂接成为普通 `UserMessage`。全程无自动提交；raw audio 不落客户端存储；STT 关闭时文本 Agent 功能完全不受影响（M7 exit 后半句）。

## 2. 非目标

- **Web 桌面端与桌面 Tauri 录音**：语音能力只在 android/ios 注入 runtime，Web/桌面不渲染语音按钮（计划 1028 行只要求 mobile）。
- speaches 容器/profile、`SpeachesTranscriptionProvider`（M7a 已完成）；upload API、`TranscriptDraft` 状态机、raw-audio 删除契约（M7.1 已完成）零改动。
- **M4.5 submit 端点的 B 侧实现**（属 M4.5）；本 spec 只钉死客户端调用契约与 `draft_ref` 的 B 侧 CAS 语义要求（§4.8）。
- **M6/M6b Agent Chat UI 本身**：本 spec 交付可嵌入的语音组件 + 嵌入契约；composer 挂载归 M6/M6b（Web 侧 chat UI 设计见 `2026-08-13-m6b-web-agent-chat-design.md`，本 spec 与它共享 `components/agent/` 目录但不重叠组件）。
- 浏览器 `SpeechRecognition` 便捷适配器（计划 §14 明确非便携核心路径）。
- 上传**百分比**进度条（invoke 通道无流式进度；用不确定态，见 §4.6）。
- 权限设置深链跳转（引导文案即可，YAGNI）。
- 录音回放、转写历史、每请求语言选择、SSE 流式转录。

## 3. 背景与现状

### 3.1 B 侧既有契约（M7.1/M7a，本 spec 不改）

- **上传** `POST /api/v1/agent/transcription/drafts`（multipart：`audio` 文件 + `binding_id` + `target_conversation_id`，`require_admin` 权限，native 客户端以 `native_client_id` 为 actor）：大小 10 MiB（Content-Length + 流式双检）；格式 webm/wav/mpeg 按 magic 嗅探（不信任声明 Content-Type）；WAV 解码前检查采样率 8-96 kHz、时长 ≤10 分钟；解码后 `duration_seconds` 复核；并发 4；B 侧 60s 墙钟；draft TTL 1 小时。错误码：`404 binding_not_found/conversation_not_found/draft_not_found`、`422 binding_conversation_mismatch/invalid_audio/audio_sample_rate_out_of_bounds/audio_too_long`、`413 audio_too_large`、`415 unsupported_audio_format`、`502 transcription_failed`、`503 speech_to_text_unavailable`、`504 transcription_timeout`、`409 draft_not_confirmable/draft_not_cancellable`、`410 draft_expired`、`403 draft_not_owner`。
- **draft 生命周期**：`POST /drafts/{id}/confirm`（204）、`POST /drafts/{id}/cancel`（204）、`GET /drafts/{id}`（仅元数据：state/provider/region/transcript_hash/expires_at，**不返回 transcript 文本**）。transcript 文本只在**上传响应中返回一次**，此后只存 B 侧哈希——客户端必须自行保留文本供编辑/确认（§4.7.4 的会话恢复设计即由此推导）。
- **draft→message**：协议 `UserMessagePayload` 已带 `draft_ref: str | None`（1..256），client-contracts `generated.ts` 已有该字段；draft 状态机含 `consumed` 终态（计划 §14）。M4.5 submit 端点 `POST /api/v1/agent/conversations/{id}/messages`（202 语义）**仅完成设计、未实现**，且其 `draft_ref` CAS 语义尚无任何文档——§4.8 补齐。
- **能力发现**：`GET /api/v1/agent/capabilities` 目前只有 `agent_broker_enabled` + `delegated_write_grants_enabled`，**没有 STT 能力旗标**；client-contracts 的 `generated.ts` 也尚无 transcription 相关类型（`generate.py` 显式 import 公开模型，需增补）。

### 3.2 客户端现状

- `apps/clients/tauri` 是唯一移动端目标（`tauri.android.conf.json`/`tauri.ios.conf.json`；CI 用 `tauri [android|ios] init --ci` 在**全新环境**重新生成 `src-tauri/gen/`，该目录被 gitignore）。
- 无任何录音/麦克风代码；mobile capability 无音频权限（`capabilities/mobile.json` 仅 core/os/store/clipboard/deep-link/opener）。
- `tauriHttpTransport` → Rust `native_http_request`（auth.rs 893 行）：**JSON-only**，reqwest + DPoP + `assert_http_target`（同 origin、`/api/` 前缀）+ nonce 重试。多 part 上传无法走该通道，需新增 Rust 命令（§4.5）。
- `client-core` 有 `api/agents.ts`（`request` 助手 + 错误信封解析 `error.code`/`status`）、`agent/stream.ts`；`client-ui` 有 `ClientRuntime`（runtime 注入：api/clock/visibility/capabilities/platform）、`BottomToast`、`components/{common,terminal,...}`、a11y/responsive contract 测试先例。M6b（Web Agent Chat）已设计共享 `components/agent/`（含 `AgentComposer.vue`）但尚未实现；Tauri 移动端 chat 视图随 M6 其余任务落地。
- Rust 测试先例：auth.rs 内 `#[cfg(test)] mod tests` + `src-tauri/tests/http_capability_scope.rs`（capability 文档契约断言）。

### 3.3 Tauri 移动端音频捕获调研结论（决策性记录）

以下结论基于 2026-08-13 的官方资料与**本仓库已 pin 的依赖源码**（Cargo.lock：`tauri 2.11.5`、`tauri-utils 2.9.3`、`wry 0.55.1`）核对，全部为可复核证据：

1. **Tauri 官方插件无录音能力**（v2.tauri.app/plugin 官方清单共 30 个：autostart/barcode/biometric/…/websocket/window-state，无 audio/mic/record 类）；社区插件中亦无被广泛维护、支持 android+ios 双端的录音插件（awesome-tauri 列表核对无）。**结论：不采纳社区插件，不新增运行时依赖；核心路径 = WebView `getUserMedia` 捕获。**
2. **Android 可行**：wry 0.55.1 的 `RustWebChromeClient.kt`（94-119 行，源自 Capacitor 同款实现）实现 `onPermissionRequest`：`AUDIO_CAPTURE` 资源 → 经 ActivityResult API 请求 `RECORD_AUDIO` + `MODIFY_AUDIO_SETTINGS`，授权后 `request.grant()`，拒绝则 `deny()`。即：**只要 app manifest 声明这两个权限，getUserMedia 的权限对话框/拒绝路径由 wry 原生处理**，WebView 层无额外原生代码。manifest 声明是本 spec 必须处理的平台配置项（§4.10，因 `gen/` 在 CI 重新生成）。
3. **iOS 可行但有格式硬约束**：WKWebView `getUserMedia` 自 iOS 14.3+ 可用；但 iOS 的 `MediaRecorder` 只产出 `audio/mp4`（AAC），**B 只接受 webm/wav/mpeg → iOS 用 MediaRecorder 必然 415**。因此 iOS 采用 PCM 捕获 + JS WAV 编码（16 kHz 单声道 16-bit，B 的 WAV 头校验可解析：fmt 块 PCM、8-96 kHz）。`AudioWorklet` 在 WKWebView 14.5+ 可用；`Info.plist` 必须声明 `NSMicrophoneUsageDescription`。
4. **Tauri IPC 不能传原始字节（Android）**：`tauri 2.11.5` `src/ipc/mod.rs` 明确注释 Android 不支持 `InvokeBody::Raw`，并建议 "consider passing raw bytes as a base64 String"。**结论：音频经 base64 字符串传参（双端统一，不做平台分支）**；`base64 0.22.1` 已是 Rust 依赖。
5. **iOS 平台配置有关键支持**：`tauri-utils 2.9.3` 的 `IosConfig` 有 `info_plist: Option<PathBuf>`（"merge with the default Info.plist"，且自动发现同目录 `Info.plist`/`Info.ios.plist`）与 `minimum_system_version`。**Android 的 `AndroidConfig` 无任何 permissions 字段**（只有 minSdkVersion/versionCode/autoIncrementVersionCode/debugApplicationIdSuffix）→ Android 权限只能靠生成后补丁脚本（§4.10）。
6. **MediaRecorder webm/opus**：Chrome for Android/Android WebView 151+ 支持 `MediaRecorder` 且 `isTypeSupported('audio/webm;codecs=opus')` 为真；EBML 头与 B 的 magic 嗅探（`\x1a\x45\xdf\xa3`）吻合。作为纵深，两平台共享同一 PCM/WAV 实现作为 fallback（§4.3.3）。

**验证状态标记**（沿用 M7a/M8 的诚实标记约定）：上述 2/3/6 的**真机行为**（Android WebView 实际产出 webm、iOS 实际捕获 PCM、权限对话框拒绝路径）在本 spec 阶段标为 `unverified`，M7 exit 真机矩阵逐项转正；纯 JS 部分（WAV 编码器、状态机、错误映射）可单测全覆盖。

## 4. 架构设计

### 4.1 总数据流（不变式）

```text
VoiceInputButton（composer 内，仅 android/ios + STT 启用 + 设备支持时渲染）
  └─ press/hold 手势状态机（client-core，纯逻辑）
       └─ AudioRecorder（tauri 适配层：getUserMedia + MediaRecorder/PCM-WAV）
            └─ native_upload_audio（Rust 命令，base64 + multipart + DPoP）
                 └─ B /api/v1/agent/transcription/drafts（201 → draft_id + transcript 文本仅此一次）
                      └─ TranscriptDraftSheet（展示/编辑/披露）
                           ├─ [放弃] → POST cancel（204）→ 关闭，绝不提交
                           └─ [确认发送] → POST confirm（204）→ POST messages {text, draft_ref}（202，M4.5）
                                └─ 普通 UserMessage（M6 事件流渲染；本 spec 只到 submit 调用成功）
```

不变式：transcript 文本只存在于客户端内存/sessionStorage（非持久化存储、非日志）；raw audio 只在内存 Blob，上传响应返回即释放；无任何代码路径在显式确认前调用 submit。

### 4.2 组件归属与注入边界

沿用仓库既有分层（runtime 注入 port，client-core 纯逻辑，client-ui 组件渲染）：

- **client-core**：`voice/recorderPort.ts`（port 类型 + 错误类）；`voice/voiceDraftController.ts`（press/hold 状态机 + 上传/确认/取消/提交编排，全部依赖注入：recorder、uploader、draft API、submit、clock、storage、logger——vitest 用 fake 全测）；`api/transcription.ts`（confirm/cancel/get 的 JSON 路径，走既有 `HttpTransport`）。
- **client-ui**：`components/agent/VoiceInputButton.vue`、`components/agent/TranscriptDraftSheet.vue`、`composables/useVoiceDraft.ts`（把 controller 状态映射为 Vue 响应式）。
- **tauri client**：`adapters/tauriVoiceRecorder.ts`（getUserMedia + 平台选择）、`adapters/pcmWavEncoder.ts`（纯函数 WAV 编码）、`adapters/tauriAudioUpload.ts`（base64 + invoke + 错误映射）；`runtime.ts` 注入。
- **tauri Rust**：`src/audio_upload.rs`（`native_upload_audio` 命令）。

`ClientRuntime` 增加可选 `voice` 能力对象：

```ts
voice?: {
  readonly uploadAudio: AudioUploader          // base64 + invoke，见 §4.5
  readonly createRecorder: () => AudioRecorder  // getUserMedia 捕获，见 §4.3
  readonly enabled: () => boolean              // platform∈{android,ios} && 设备特性可用
}
```

语音按钮渲染条件（缺一即不渲染，文本 Agent 功能不受影响）：`runtime.voice` 存在 ∧ `voice.enabled()` ∧ `capabilities.speech_to_text_enabled`（§4.9）。Web/桌面 runtime 不注入 `voice` → 按钮天然消失。

### 4.3 录音适配器

#### 4.3.1 port（client-core）

```ts
interface RecordedAudio { blob: Blob; mimeType: 'audio/webm' | 'audio/wav'; durationSeconds: number }
interface AudioRecorder {
  start(): Promise<void>              // 权限未决/拒绝时 reject PermissionDeniedError | RecorderUnavailableError
  stop(): Promise<RecordedAudio>      // 停止并返回内存 Blob；重复调用幂等
  abort(): void                       // 取消并丢弃（释放 tracks）
}
```

#### 4.3.2 平台选择（tauriVoiceRecorder）

- `start()`：`getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, channelCount: 1}})`（每会话获取，stop/abort 时释放全部 tracks——避免常驻"录音中"系统指示）。
- **Android**：`MediaRecorder.isTypeSupported('audio/webm;codecs=opus')` → MediaRecorder 产出 webm；不支持则回落 PCM/WAV。
- **iOS**：直接走 PCM/WAV（`AudioWorklet` 16 kHz 单声道重采样 + 编码器）；`addModule` 失败回落 `ScriptProcessorNode`（同编码器，牺牲少量时序质量）。
- 任何平台捕获异常分类：`NotAllowedError`/`SecurityError` → `PermissionDeniedError`；`NotFoundError`/`NotReadableError`/其他 → `RecorderUnavailableError`。

#### 4.3.3 PCM/WAV 编码器（纯函数，可单测）

16 kHz 单声道 16-bit PCM WAV：标准 44 字节头（RIFF/fmt[audio_format=1, channels=1, sample_rate=16000, byte_rate=32000, block_align=2, bits=16]/data），全部小端；与 B `_wav_sample_rate_duration` 的解析（fmt 块 ≥16 字节、PCM、采样率 8-96 kHz、byte_rate>0）逐项吻合。时长预算：16 kHz mono = 1.92 MB/min → 240s 上限 = 7.68 MB < 10 MiB（§4.4 的时长上限即由此定）。

### 4.4 press/hold 手势状态机（client-core 纯逻辑）

状态与转移（时钟、recorder、uploader 全部注入，便于 fake 测试）：

```text
idle
 ├─ pointerdown/keydown(空格或回车) → arming
 │    ├─ start() 成功 → recording（启动计时器；视觉：红点脉冲 + 秒表 + "上滑取消"提示）
 │    ├─ PermissionDeniedError → denied（toast 文案"无法访问麦克风，请在系统设置中允许 TermFlow 使用麦克风后重试"+ 按钮保持可重按）
 │    ├─ RecorderUnavailableError → unavailable（toast + 隐藏按钮）
 │    └─ 释放发生在 start() 完成前 → idle（放弃本次，不开始录音）
 ├─ recording 期间：
 │    ├─ pointerup/keyup（未进入取消区）→ stopping
 │    │    ├─ 时长 < 0.5s → tooShort（丢弃 + toast "录音太短"）
 │    │    └─ 时长 ≥ 0.5s → uploading（上传态 §4.6）
 │    ├─ pointermove 上滑 > 96px（视觉滞回，回到 48px 内恢复）→ cancelPending（取消区高亮）
 │    │    └─ pointerup → cancelled（丢弃，无上传）
 │    ├─ pointercancel / visibilitychange(hidden)（系统手势/来电/退后台）→ cancelled
 │    └─ 时长 ≥ 240s → 自动 stop → uploading（边界保护：16kHz WAV 240s=7.68MB<10MiB；B 侧 10 分钟上限不是客户端的约束上限）
 └─ uploading → draft（draft sheet）| failure（错误态，§4.6）| abortedByUser（放弃上传）
```

要点：`pointerdown/up/cancel`（而非 touch 事件）同时覆盖触屏/鼠标/笔；滑动取消阈值与滞回防止抖动；上传中允许"取消上传"（AbortSignal；B 侧可能已建 draft → 1 小时 TTL 自然过期，客户端不清理，记录为决策）。

### 4.5 上传通道：Rust `native_upload_audio`

`native_http_request` 是 JSON-only，无法表达 multipart；且 M6 已确立"WebView 网络权威归 Rust"。新增命令（`src/audio_upload.rs`，签名与既有命令同模式）：

```rust
#[tauri::command]
async fn native_upload_audio(
    state: State<'_, NativeAuthState>,
    issuer: String,
    audio_base64: String,      // base64 字符串（§3.3.4：Android 不支持 InvokeBody::Raw，双端统一）
    mime: String,              // 白名单 "audio/webm" | "audio/wav"（客户端绝不发 mpeg）
    binding_id: String,
    conversation_id: String,
    nonce: Option<String>,
) -> Result<NativeHttpResponse, String>
```

契约：

- `canonical_issuer` + `assert_http_target(&issuer, "/api/v1/agent/transcription/drafts")`（同 origin、`/api/` 前缀——沿用既有 URL 白名单，不新增网络权威面）。
- base64 解码（`base64 0.22.1`，已有依赖）→ 字节；防御边界 `0 < len <= 10 MiB + 64 KiB`（客户端 240s WAV 7.68 MB 的双保险），超限 `safe_error("audio_too_large")`。
- `mime` 白名单映射文件名：`audio/webm→speech.webm`、`audio/wav→speech.wav`，其余 `safe_error("audio_mime_not_allowed")`。
- reqwest `multipart`：`file`（bytes + filename + content-type）、`binding_id`、`target_conversation_id` 文本字段；不手工设置 Content-Type（reqwest 自动生成 boundary；B 以 Content-Length 做首道检查，reqwest 自动携带）。
- DPoP 与 nonce 重试循环**逐行复用** `native_http_request` 的 send 闭包模式（`dpop_proof` 只签 htu/htm，与 body 无关）。
- reqwest 超时 120s（> B 的 60s 墙钟，让 B 的 504 先到而不是客户端先断）；错误 → `safe_error("offline")`。
- 响应原样返回 `NativeHttpResponse { status, headers, body }`，由 JS 层解析（201 JSON → draft 响应；错误信封 → status/code，复用 client-core 的 `publicErrorDetails` 语义）。

### 4.6 上传状态与错误文案映射（客户端）

上传态为**不确定态**（无百分比进度——invoke 通道无流式进度，10 MiB 上限下进度条收益低于成本）：spinner + "正在转写…" + 已用秒数。客户端 AbortSignal 超时 120s（与 Rust reqwest 超时一致）。

| 触发 | 文案（中文） | 语义 | 动作 |
|---|---|---|---|
| 503 `speech_to_text_unavailable` | "本部署未启用语音转写" | 能力旗标竞态 | toast + 本会话禁用按钮（下次启动重查旗标） |
| 502 `transcription_failed` | "转写失败，请重试" | provider 瞬态 | 保留 blob，[重试] 重新上传同一音频 |
| 504 `transcription_timeout` | "转写超时，请重试" | B 60s 墙钟 | 同上 |
| 413 `audio_too_large` | "录音超出大小限制" | 不应发生（240s 上限） | 丢弃 blob，[重新录音] |
| 415 `unsupported_audio_format` | "录音格式不受支持" | 不应发生（客户端白名单） | 丢弃，[重新录音] + 诊断日志 |
| 422 `invalid_audio`/`audio_sample_rate_out_of_bounds`/`audio_too_long` | "录音无法识别，请重新录制" | B 解码边界 | 丢弃，[重新录音] |
| 离线 / `offline` / 请求中止 | "网络不可用，请重试" | 传输层 | 保留 blob，[重试] |
| `http_capability_denied` | "网络能力受限" | Rust 白名单拒绝 | 诊断日志 + 本会话禁用按钮 |

重试语义：**5xx/离线保留 blob 可重试（同一音频）**；**4xx 丢弃 blob 只能重录**（4xx 对同一输入必然重现）。重试不设自动次数上限但每次重试需用户点击（不自动循环）。

### 4.7 draft sheet UI

#### 4.7.1 结构与交互

上传 201 后弹底部 sheet（app 级模态，非逐视图）：

- `textarea`（预填 transcript，可编辑；`maxlength` 对齐协议 `MAX_AGENT_TEXT_BYTES`，超限前端提示；label："转写文本（可编辑）"）。
- **披露行**（计划 §14 provider disclosure）："由本部署语音转写服务转写 · 提供方 {provider}"（`region`/`language`/`duration_seconds` 非空时追加；M7a 下 region 为空则省略）。
- 按钮：[放弃] [确认发送]。
- **放弃** → `POST cancel`（204）→ 关闭。cancel 的 409/410/404 一律静默关闭（best-effort；服务端 draft 由 TTL 兜底）。
- **确认发送** → §4.8 序列 → 关闭 + toast "已发送"（消息本体经 M6 事件流出现）。
- **退出 sheet 不经按钮**（back 手势/Esc）→ 弹确认对话框"放弃本次转写？"（确认 → cancel 调用；取消 → 留在 sheet）——保证"放弃是显式动作"，同时避免服务端 draft 垃圾。
- 确认/提交错误按 §4.8 的错误表处理；`draft_expired` → toast "转写已过期，请重新录音" + 关闭。

#### 4.7.2 不自动提交不变式的客户端体现

controller 的 API 面不暴露"直接提交"路径：`submit` 仅在 `confirm` 204 成功后的同一序列中可调用；单元测试断言：cancel/关闭/后台/过期路径下 `submit` 调用次数恒为 0（§6.1）。

#### 4.7.3 组件嵌入契约（M6 依赖）

`VoiceInputButton` props：`{ bindingId: string; conversationId: string; speechToTextEnabled: boolean }`（父级取 `capabilities()` 传入，与 §4.2 渲染条件一致），emit：`draft-submitted(draftId)`。M6b 设计的共享 `AgentComposer.vue` 在移动端布局挂入该按钮（Web/桌面不挂）；`TranscriptDraftSheet` 无 props（从注入的 controller 取状态），emit：`closed`、`submitted`。直到 M6b/M6 落地，两组件由组件测试 + controller 单测覆盖，不产生运行时集成点。

#### 4.7.4 draft 会话恢复（transcript 文本只返回一次）

B 不持久化 transcript 文本（只存哈希），app 被杀则文本丢失。缓解：draft 创建后把 `{ draftId, text, bindingId, conversationId, provider, region, expiresAt }` 写入 **sessionStorage**（键 `termflow.voice.draft`，单槽；文本而非音频，不落持久化存储）；应用重载后，目标会话的 composer 显示"待确认转写"chip，点按重开 sheet（文本从 sessionStorage 恢复）。`expiresAt` 已过 → 丢弃并 toast。confirm/cancel 完成 → 清除该键。内存 Blob 不做任何持久化（raw-audio 客户端不落盘）。

### 4.8 confirm → submit 接线（draft→UserMessage，M4.5 依赖契约）

**客户端序列**（唯一路径，controller 内固化）：

```text
[确认发送] → POST /drafts/{draftId}/confirm (204)
          → POST /conversations/{conversationId}/messages  body={ text: 编辑后的文本, draft_ref: draftId }  (202)
```

**本 spec 钉死的 B 侧 CAS 语义要求**（M4.5 实现 submit 端点时必须满足；客户端按此契约实现与测试）：

1. submit 收到 `draft_ref` 时原子校验：draft 存在 ∧ owner==当前 actor ∧ state==`confirmed` ∧ `target_conversation_id`==URL 会话 ∧ 未过期；任一不满足 → 拒绝且**不产生 admission**。
2. 校验通过 → draft CAS `confirmed→consumed` 与 inbox admission **同事务**（一次性消费，重放同 `draft_ref` 不得产生第二条消息）。
3. 文本不设哈希强校验（用户编辑后文本与存储哈希必然不同；哈希只作审计元数据），但提交文本走既有 plain-text 校验。
4. 错误语义（客户端按此映射）：`410 draft_expired` → 重录；`409 draft_not_consumable`（含已 consumed）→ 视为"已发送"（幂等成功处理，toast"已发送"）；`422 draft_conversation_mismatch` → 诊断日志 + toast 通用错误。

**客户端提交失败语义**：confirm 成功后 submit 网络失败 → 保留 sessionStorage，draft 仍是 confirmed → 重试 submit（不重发 confirm）；收到 409 consumed → 清除存储、按成功处理（防重复提交由 B 的一次性 CAS 保证，客户端以 `draft_ref` 幂等）。submit 未实现前的测试用 fake（§6.1），B 侧联调挂 M4.5/M7 exit。

### 4.9 能力发现（B 侧最小改动）

语音按钮需要预知 STT 是否可用（先录音再 503 是坏体验）。在 `agent_capabilities.py` 增加：

```python
class AgentCapabilitiesResponse(BaseModel):
    """Agent Broker capability-discovery payload consumed by C (M7b adds STT)."""

    agent_broker_enabled: bool
    delegated_write_grants_enabled: bool = False
    speech_to_text_enabled: bool = False

@router.get("/capabilities", response_model=AgentCapabilitiesResponse)
async def get_agent_capabilities(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentCapabilitiesResponse:
    provider = getattr(request.app.state, "transcription_provider", None)
    return AgentCapabilitiesResponse(
        agent_broker_enabled=settings.agent_broker_enabled,
        delegated_write_grants_enabled=settings.agent_delegated_write_grants_enabled,
        speech_to_text_enabled=provider is not None and provider.available(),
    )
```

- 语义：配置派生（M7a §5.3：`available()` 无实时探测）——默认 NullProvider → False；启用 Speaches → True。
- 这是本 spec **唯一的 B 侧 API 变更**（M7.1 upload API、draft 状态机零改动），依据 M1 先例"能力发现端点承载 UI 可见性旗标"。
- `scripts/generate-client-contracts/generate.py` 增 import `TranscriptionDraftResponse`/`TranscriptionDraftDetailResponse`（响应类型进 `generated.ts`；multipart 请求体手工构造，不进契约），重生成后 `npm run contracts:check`。
- 竞态兜底：旗标 True 但上传 503 → §4.6 文案 + 本会话禁用按钮（一次失败后本地禁用至下次启动重查旗标，不做轮询）。

### 4.10 移动端平台配置

- **iOS**（配置化，无需补丁脚本）：`tauri.ios.conf.json` 的 `bundle` 增 `{ "iOS": { "minimumSystemVersion": "15.0", "infoPlist": "Info.ios.plist" } }`；新增被跟踪文件 `apps/clients/tauri/src-tauri/Info.ios.plist`，仅含 `NSMicrophoneUsageDescription`（中文文案："用于按住说话并将语音转写为文字，录音仅上传到您连接的 TermFlow 服务器"）。15.0 覆盖 getUserMedia（14.3+）与 AudioWorklet（14.5+）并留余量。
- **Android**（`tauri.android.conf.json` 无权限键，§3.3.5）：新增 `scripts/patch-mobile-manifests.py`（幂等：向 `gen/android/app/src/main/AndroidManifest.xml` 注入 `<uses-permission android:name="android.permission.RECORD_AUDIO"/>` 与 `MODIFY_AUDIO_SETTINGS`，已存在则跳过）；CI（`ci.yml`、`tauri-packages.yml`）在 `android init --ci` 之后执行；本地开发文档（README）注明 `tauri android init` 后运行一次。`capabilities/mobile.json` 无需变更（录音是 WebView 级媒体捕获 + 应用自有命令，不涉及 Tauri 插件权限；新增 Rust 命令沿用 core:default 的既有模式）。
- CSP 无需变更（无 WebView 网络请求、无 blob/worker 脚本加载需求——iOS 的 AudioWorklet 模块经 Vite 打包为同源资产，`script-src 'self'` 覆盖）。

### 4.11 可访问性

- **VoiceInputButton**：`role="button"`、`aria-label="按住说话"`；键盘等价操作：`keydown`(Space/Enter) 开始（对这两个键调用 `preventDefault`，避免 keyup 触发原生 click 造成双重激活）、`keyup` 结束；录制中 `aria-pressed=true` + 视觉红点脉冲（`prefers-reduced-motion` 下静态红点）；`aria-describedby` 指向"按住说话，松开发送，上滑取消"提示。所有状态播报走 `aria-live="polite"` 区域（"录音中"/"正在转写"/"转写完成，请确认"），错误走 `role="alert"`（复用 BottomToast 的既有语义）。
- **TranscriptDraftSheet**：`role="dialog"` + `aria-modal` + 焦点陷阱（初始焦点 textarea）+ Esc 等价"退出"路径（§4.7.1 的确认对话框）；textarea 显式 label；按钮为原生 `<button>`；对比度走 design-tokens。
- 触控目标 ≥ 44×44px（WCAG 2.5.5 目标尺寸下限）。

## 5. 文件变更

**新增：**

| 文件 | 内容 |
|---|---|
| `packages/client-core/src/voice/recorderPort.ts` | `AudioRecorder`/`RecordedAudio` port + `PermissionDeniedError`/`RecorderUnavailableError`（§4.3.1） |
| `packages/client-core/src/voice/voiceDraftController.ts` | press/hold 状态机 + 上传/确认/取消/提交编排 + sessionStorage 恢复（§4.4/4.6/4.7/4.8），全依赖注入 |
| `packages/client-core/src/voice/voiceDraftController.test.ts` | 控制器单测（§6.1 矩阵） |
| `packages/client-core/src/api/transcription.ts` | `confirmDraft`/`cancelDraft`/`getDraft`（JSON 走既有 `HttpTransport`）；上传不在此（走注入 uploader） |
| `packages/client-core/src/api/transcription.test.ts` | 端点路径/错误解析测试 |
| `packages/client-ui/src/components/agent/VoiceInputButton.vue` + `.test.ts` | press/hold 按钮 + a11y（§4.11） |
| `packages/client-ui/src/components/agent/TranscriptDraftSheet.vue` + `.test.ts` | draft sheet（§4.7）+ a11y |
| `packages/client-ui/src/composables/useVoiceDraft.ts` | controller → Vue 响应式桥 |
| `apps/clients/tauri/src/adapters/tauriVoiceRecorder.ts` | getUserMedia + 平台选择（MediaRecorder webm / PCM-WAV）+ 错误分类（§4.3.2） |
| `apps/clients/tauri/src/adapters/pcmWavEncoder.ts` + `.test.ts` | 纯函数 16kHz mono WAV 编码器（§4.3.3），字节级断言 |
| `apps/clients/tauri/src/adapters/tauriVoiceRecorder.test.ts` | mock `navigator.mediaDevices`/MediaRecorder 的选择与错误分类 |
| `apps/clients/tauri/src/adapters/tauriAudioUpload.ts` + `.test.ts` | base64 + invoke + 错误信封解析（§4.5/4.6） |
| `apps/clients/tauri/src-tauri/src/audio_upload.rs` | `native_upload_audio` 命令（§4.5）+ 模块内 `#[cfg(test)]` 单测 |
| `apps/clients/tauri/src-tauri/tests/audio_upload_contract.rs` | Rust 集成测试（multipart 字段/DPoP 头/nonce 重试/错误映射，仿 auth.rs 测试先例） |
| `apps/clients/tauri/src-tauri/Info.ios.plist` | `NSMicrophoneUsageDescription`（§4.10） |
| `scripts/patch-mobile-manifests.py` | Android manifest 权限注入（幂等，§4.10）+ 自测 |

**修改：**

| 文件 | 内容 |
|---|---|
| `packages/client-ui/src/runtime.ts` | `ClientRuntime.voice?` 可选能力（§4.2） |
| `apps/clients/tauri/src/runtime.ts` | 注入 `voice`（recorder + uploader + platform gate），其余 runtime 对象不变 |
| `apps/clients/tauri/src-tauri/src/lib.rs` | 注册 `native_upload_audio` |
| `apps/clients/tauri/src-tauri/tauri.ios.conf.json` | `bundle.iOS.minimumSystemVersion`/`infoPlist`（§4.10） |
| `.github/workflows/ci.yml`、`.github/workflows/tauri-packages.yml` | 每次 `[android|ios] init --ci` 后运行 `patch-mobile-manifests.py`（iOS 为断言检查） |
| `apps/control-plane/src/termflow_control_plane/api/agent_capabilities.py` | `speech_to_text_enabled` 旗标（§4.9） |
| `apps/control-plane/tests/test_agent_registration.py` | 既有 capabilities 端点测试所在：新增旗标断言（默认 False；装配 Speaches 后 True） |
| `scripts/generate-client-contracts/generate.py` | import + 渲染 transcription 响应类型（§4.9） |
| `packages/client-contracts/src/generated.ts` | 重生成（`TranscriptionDraftResponse`/`TranscriptionDraftDetailResponse`/`speech_to_text_enabled`） |
| `README.md` | 移动端录音权限/平台配置说明（含本地开发 patch 脚本步骤） |

**不改：** `api/transcription.py`、`persistence/models.py`、迁移、`capabilities/mobile.json`、`tauri.conf.json`（CSP）、`apps/clients/web/**`、`packages/client-ui/src/views/**`（composer 挂载归 M6b）。`components/agent/` 目录与 M6b 共享（`AgentComposer.vue` 等属 M6b；本 spec 新增文件名与其不重叠）。

## 6. 测试策略

### 6.1 计划 1029 行验证矩阵逐项映射（客户端侧断言）

| 矩阵项 | 客户端侧断言 | 层/测试位置 |
|---|---|---|
| permission denial | recorder mock reject `PermissionDeniedError` → 显示权限引导文案、无上传调用、按钮可重按；`getUserMedia` 真实 NotAllowedError 分类单测 | controller 单测 + tauriVoiceRecorder 单测 + 组件测试 |
| format limits | 适配器只产出 webm/wav（mime 白名单断言）；uploader 拒绝其他 mime；B 415 → 文案+[重新录音] | pcmWavEncoder/tauriAudioUpload/controller 单测 |
| size limits | 240s 时长上限使 16kHz WAV ≤7.68MB <10MiB（编码器字节数断言）；上传前 `blob.size` 防御检查；B 413 → 文案 | controller/encoder 单测 |
| duration limits | 240s 自动 stop（fake clock）；<0.5s 丢弃；B 422 `audio_too_long` → 文案+[重新录音] | controller 单测（fake timers） |
| timeout | 客户端 AbortSignal 120s 中止 → "网络不可用"可重试；B 504 → "转写超时"可重试；Rust 侧超时常量 120s 与 B 侧 60s 墙钟的层级关系以耦合注释固化（同 M7a §5.1 惯例） | controller 单测 + Rust 命令超时单测 |
| provider absence | 旗标 False → 按钮不渲染；旗标竞态 503 → toast+本会话禁用 | 组件测试（fake runtime）+ controller 单测 |
| raw-audio deletion（客户端视角） | upload 完成/失败后 blob 引用释放（GC 可达性断言：controller 不再持有）；storage/诊断日志只含文本与元数据，**断言无音频字节写入** | controller 单测（fake storage/logger 记录调用） |
| transcript-not-auto-submit | 未点[确认发送]时 `submit` 调用数恒 0（cancel/关闭/后台/过期/重试路径逐一断言）；确认序列唯一路径 confirm(204)→submit(202) | controller 单测（fake submit spy） |

### 6.2 组件测试（Vitest + @vue/test-utils，jsdom）

- VoiceInputButton：渲染条件（三条件缺一不渲染）；pointerdown→up 短按丢弃；上滑取消（模拟 pointermove/up 序列 + 96px 阈值与滞回）；240s 自动停止（fake timers）；键盘 Space/Enter 按下-抬起；aria-pressed/aria-live 播报；权限拒绝文案。
- TranscriptDraftSheet：编辑后提交携带编辑文本；[放弃] 调 cancel 且不调 submit；Esc/back 确认对话框两分支；披露行渲染（provider 存在/region 为空两种）；410 过期文案；sessionStorage 恢复重开。
- a11y：既有 `a11y-contract.test.ts` 模式（dialog 焦点陷阱、label 存在、alert 语义）。

### 6.3 Rust 测试

- `audio_upload.rs` 模块内单测：base64 解码边界（空/超限/非法）、mime 白名单→文件名映射、multipart 字段构造（reqwest 无法注入 mock，抽出纯函数 `build_multipart(bytes, mime, binding_id, conversation_id) -> reqwest::multipart::Form` 做字段断言；目标 URL/DPoP 头断言复用 `native_http_request` 既有测试手法）、错误映射 safe_error 系列。
- `tests/audio_upload_contract.rs`：目标路径必须为 `/api/v1/agent/transcription/drafts`（`assert_http_target` 语义：同 origin、`/api/` 前缀、拒绝绝对 URL/反斜杠）；上传尺寸上限常量与 B 的 10 MiB 一致（常量注释互指）。
- 既有 `tests/http_capability_scope.rs` 保持通过（不新增 capability 权限）。

### 6.4 平台配置测试

- `patch-mobile-manifests.py` 自测（临时 fixture manifest：注入幂等、已存在跳过、缺失时报错退出码）。
- CI：patch 后、构建前断言 `gen/android/app/src/main/AndroidManifest.xml` 含 `RECORD_AUDIO` 与 `MODIFY_AUDIO_SETTINGS`（CI-gated 检查步骤）；iOS 构建后以 `plutil`/文本断言产物 Info.plist 含 `NSMicrophoneUsageDescription`。

### 6.5 契约测试（B 侧小改动）

- `test_agent_registration.py`（既有 capabilities 测试所在）：默认 settings → `speech_to_text_enabled=False`；`stt_enabled=True` 装配 → True。
- `contracts:check`：generated.ts 含新类型与旗标（CI 既有步骤覆盖）。

### 6.6 M7 exit 真机验证（本 spec 之外，标记为集成项）

Android 真机：webm 实际产出与上传、权限对话框允许/拒绝两分支、上滑取消手势手感；iOS 真机：PCM/WAV 捕获、`NSMicrophoneUsageDescription` 文案、AudioWorklet 在 WKWebView 的模块加载。任一失败不推断为通过（M8 契约）；失败时按 §7 风险 1 的升级路径处理。

## 7. 风险与缓解

1. **WebView 音频捕获在真机未经验证**（Android WebView 的 MediaRecorder 与 iOS WKWebView 的 AudioWorklet 均无真机证据）。缓解：纯 JS 逻辑全单测覆盖 + 特性探测阶梯（MediaRecorder webm → AudioWorklet WAV → ScriptProcessorNode WAV）；M7 exit 真机矩阵逐项转正；若 iOS 捕获在真机失败，升级路径为 ScriptProcessorNode（已实现为回落）→ 仍失败则记录为 "iOS 语音 unverified" 并保持按钮隐藏，**绝不静默降级**（沿用 M8 诚实标记约定）。
2. **Android manifest 权限注入脆弱**（`gen/` 被 gitignore 且 CI 每次全新生成，手工改 manifest 必丢）。缓解：幂等补丁脚本 + CI 构建前契约断言（§6.4）+ README 本地步骤；脚本失败 fail-loud（非零退出码阻断构建）。
3. **base64 IPC 开销**（7.68MB WAV → ~10.2MB base64 字符串经 JSON IPC，Android 不支持 Raw 字节）。缓解：240s 时长上限把最坏体积约束在 10MiB 量级；真机测 IPC 序列化时延；若超出可接受范围，升级路径为 Android 侧改用文件系统传递（写 app cache + 路径传参 + 用后即删）——本 spec 不预先实现。
4. **transcript 文本只返回一次，app 被杀丢文本**（B 只存哈希是安全设计，不可改）。缓解：sessionStorage 恢复（§4.7.4）+ 过期提示重录；文本不落持久化存储，隐私边界不变。
5. **M4.5 submit 端点未实现 + `draft_ref` CAS 语义空白**。缓解：§4.8 钉死客户端契约与 B 侧语义要求（原子 consume、一次性、409/410/422 映射），作为 M4.5 实现的强制输入；客户端用 fake submit 先行实现与测试，联调挂 M4.5。
6. **确认后提交失败导致重复消息**（confirm 成功、submit 网络失败、用户重试）。缓解：一次性 CAS（B）+ 客户端 `draft_ref` 幂等 + 409 consumed 视为成功；绝不自动重发（对齐计划 §6.1 no-silent-duplicate-action 精神）。
7. **能力旗标与真实可用性竞态**（旗标 True 但上传 503）。缓解：503 文案 + 单次失败后本会话隐藏按钮，不做轮询探测（YAGNI，M7a §5.3 决策一致）。
8. **上传中 app 退后台**（移动端常见）。缓解：AbortSignal 中止 → 保留 blob 可重试；B 侧可能已建 draft，由 1h TTL 自然过期，客户端不清理远端（记录为决策）；录音中退后台由 pointercancel/visibilitychange 走取消路径。

## 8. 决策摘要

- **捕获路径**：WebView `getUserMedia` 为核心（不采纳社区插件、无新运行时依赖）；Android = MediaRecorder `audio/webm;codecs=opus`；iOS = AudioWorklet PCM → 16 kHz mono 16-bit WAV（iOS MediaRecorder 只产 mp4，B 拒收故不可用）；共享 PCM/WAV 编码器作双端 fallback。
- **权限**：Android 依赖 wry 0.55.1 既有 `onPermissionRequest`（RECORD_AUDIO + MODIFY_AUDIO_SETTINGS，manifest 由补丁脚本注入）；iOS 依赖 `bundle.iOS.infoPlist` 合并 + `NSMicrophoneUsageDescription`，`minimumSystemVersion=15.0`。
- **上传**：新增 Rust 命令 `native_upload_audio`（base64 字符串入参——Android IPC 不支持 Raw 字节；multipart；DPoP/nonce 复用既有模式；reqwest 超时 120s > B 60s）；不做百分比进度（不确定态）。
- **时长/大小**：客户端 240s 自动停（16kHz WAV 7.68MB < 10MiB 推导）、<0.5s 丢弃；B 侧边界仍是权威。
- **错误语义**：5xx/离线保留 blob 可重试同一音频；4xx 丢弃只能重录；503 本会话禁用按钮（下次启动重查旗标）。
- **draft 会话**：sheet 显式[确认发送]/[放弃]；退出需确认对话框；sessionStorage 恢复（单槽）；transcript 文本仅内存/sessionStorage，raw audio 仅内存且上传后即释放。
- **提交接线**：确认序列 confirm(204)→submit(202, `draft_ref`)；B 侧 CAS 语义（§4.8）作为 M4.5 强制契约；409 consumed 幂等视为成功。
- **能力发现**：B `agent_capabilities.py` 增 `speech_to_text_enabled`（配置派生，本 spec 唯一 B 侧 API 改动）+ contracts 重生成。
- **范围边界**：Web/桌面不渲染语音 UI；M6 composer 挂载、M4.5 submit 实现、speaches/provider、upload API 均不在本 spec。
- **验证**：纯逻辑/组件/Rust/契约全自动化；真机行为标 unverified，M7 exit 转正。
