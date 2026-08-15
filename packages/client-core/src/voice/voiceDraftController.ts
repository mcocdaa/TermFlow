/**
 * Press/hold voice-draft controller (M7b spec §4.4/§4.6/§4.7/§4.8).
 *
 * Pure framework-free logic with every side effect injected:
 * recorder factory, uploader, draft API, message submit, clock, scheduler,
 * draft storage and logger. The tauri runtime wires the real adapters;
 * vitest tests wire fakes (§6.1 validation matrix).
 *
 * Invariants (M7b spec §4.1/§4.7.2):
 * - The transcript text lives only in memory / session storage; raw audio
 *   lives only in memory and is released once the upload settles.
 * - There is exactly one submit path: confirm(204) followed by
 *   submit(202, draft_ref) in the same sequence. No other code path may
 *   call `submit`.
 */

import { PermissionDeniedError, type AudioRecorder, type RecordedAudio } from './recorderPort'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Single-slot key in the injected draft store for the pending draft (M7b spec §4.7.4). */
export const VOICE_DRAFT_STORAGE_KEY = 'termflow.voice.draft'

/**
 * Client-side capture ceiling: 240s of 16 kHz mono WAV is 7.68 MB, which
 * stays under the B side 10 MiB upload bound (M7b spec §4.4/§4.3.3).
 */
export const MAX_RECORDING_SECONDS = 240

/** Recordings shorter than this are dropped (M7b spec §4.4). */
export const MIN_RECORDING_SECONDS = 0.5

/** Slide-up distance that enters the cancel zone (M7b spec §4.4). */
export const CANCEL_SLIDE_THRESHOLD_PX = 96

/** Slide-up distance that leaves the cancel zone (hysteresis, M7b spec §4.4). */
export const CANCEL_RESUME_THRESHOLD_PX = 48

/**
 * Client upload timeout. Matches the Rust `native_upload_audio` reqwest
 * timeout and deliberately exceeds the B side 60s wall clock so the B 504
 * surfaces before the client gives up (M7b spec §4.5/§4.6).
 */
export const UPLOAD_TIMEOUT_MS = 120_000

/**
 * Defensive blob-size bound before upload; mirrors the B side
 * MAX_AUDIO_BYTES (10 MiB, M7b spec §4.5).
 */
export const MAX_AUDIO_BYTES = 10 * 1024 * 1024

/** Client-side mime whitelist (M7b spec §4.3.1/§4.6). */
export const ALLOWED_AUDIO_MIME_TYPES: readonly RecordedAudio['mimeType'][] = ['audio/webm', 'audio/wav']

/** User-facing copy, fixed by the M7b spec (§4.4/§4.6/§4.7.1/§4.8). */
export const VOICE_MESSAGES = {
  permissionDenied: '无法访问麦克风，请在系统设置中允许 TermFlow 使用麦克风后重试',
  recorderUnavailable: '无法使用麦克风，请检查设备后重试',
  tooShort: '录音太短',
  providerUnavailable: '本部署未启用语音转写',
  transcriptionFailed: '转写失败，请重试',
  transcriptionTimeout: '转写超时，请重试',
  audioTooLarge: '录音超出大小限制',
  unsupportedFormat: '录音格式不受支持',
  audioInvalid: '录音无法识别，请重新录制',
  offline: '网络不可用，请重试',
  capabilityDenied: '网络能力受限',
  draftExpired: '转写已过期，请重新录音',
  submitted: '已发送',
  submitFailed: '发送失败，请重试',
  confirmFailed: '无法确认转写，请重新录音',
} as const

// ---------------------------------------------------------------------------
// Ports (all injected — no framework, no globals except Blob/AbortController)
// ---------------------------------------------------------------------------

/**
 * B side upload 201 body (M7b spec §3.1). Handwritten in client-core; the
 * multipart request itself is built by the injected uploader, not here.
 */
export interface TranscriptionUploadResponse {
  draft_id: string
  state: string
  transcript: string
  provider: string
  region: string
  language: string | null
  duration_seconds: number | null
  expires_at: string
}

export interface AudioUploadParams {
  bindingId: string
  conversationId: string
  signal: AbortSignal
}

export type AudioUploader = (
  audio: RecordedAudio,
  params: AudioUploadParams,
) => Promise<TranscriptionUploadResponse>

export interface SubmitMessageParams {
  conversationId: string
  text: string
  draftRef: string
}

/**
 * M4.5 `POST /conversations/{id}/messages` with `draft_ref` (202 semantics).
 * Not implemented on the B side yet — the runtime injects a fake until M4.5
 * lands (M7b spec §4.8).
 */
export type SubmitMessage = (params: SubmitMessageParams) => Promise<void>

/** Draft lifecycle JSON endpoints (implemented in `api/transcription.ts`). */
export interface VoiceDraftApi {
  confirm(draftId: string, signal?: AbortSignal): Promise<void>
  cancel(draftId: string, signal?: AbortSignal): Promise<void>
}

export interface VoiceClock {
  now(): number
}

export interface VoiceScheduler {
  set(fn: () => void, ms: number): unknown
  clear(handle: unknown): void
}

export interface VoiceStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/** Diagnostics only — must never receive transcript text or audio bytes. */
export interface VoiceLogger {
  error(message: string, details?: Record<string, unknown>): void
}

export interface VoiceToast {
  message: string
}

export interface VoiceDraftCallbacks {
  onState(state: VoiceDraftState): void
  onToast(toast: VoiceToast): void
}

// ---------------------------------------------------------------------------
// Error classification (M7b spec §4.6/§4.8)
// ---------------------------------------------------------------------------

/** Structured error surface shared by the injected adapters. */
export interface VoiceErrorInfo {
  status?: number
  code?: string
  kind?: 'offline' | 'aborted' | 'http_capability_denied' | 'invalid_request'
}

/**
 * Extract `{status, code, kind}` from any thrown value. The tauri upload
 * adapter throws error-envelope shaped values (`{status, code}`); the JSON
 * draft API throws client-core `ApiError`s; transport failures surface
 * `kind: 'offline' | ...`.
 */
export function voiceErrorInfo(error: unknown): VoiceErrorInfo {
  if (typeof error !== 'object' || error === null) return {}
  const candidate = error as Record<string, unknown>
  const result: VoiceErrorInfo = {}
  if (typeof candidate.status === 'number') result.status = candidate.status
  if (typeof candidate.code === 'string') result.code = candidate.code
  if (
    candidate.kind === 'offline' ||
    candidate.kind === 'aborted' ||
    candidate.kind === 'http_capability_denied' ||
    candidate.kind === 'invalid_request'
  ) {
    result.kind = candidate.kind
  }
  return result
}

export interface UploadFailureMapping {
  kind: 'transient' | 'discard' | 'provider_unavailable' | 'capability_denied'
  message: string
  /** Whether the controller should emit a diagnostic log entry. */
  diagnosticLog: boolean
}

/**
 * Map an upload failure to copy/semantics/action per M7b spec §4.6:
 * 5xx/offline keep the blob and offer retry; 4xx discard the blob and only
 * allow re-recording; 503 and capability denial disable the button for the
 * session.
 */
export function classifyUploadError(error: unknown): UploadFailureMapping {
  const info = voiceErrorInfo(error)
  if (info.kind === 'http_capability_denied') {
    return { kind: 'capability_denied', message: VOICE_MESSAGES.capabilityDenied, diagnosticLog: true }
  }
  if (info.status === 503 && info.code === 'speech_to_text_unavailable') {
    return { kind: 'provider_unavailable', message: VOICE_MESSAGES.providerUnavailable, diagnosticLog: false }
  }
  if (info.status === 413) {
    return { kind: 'discard', message: VOICE_MESSAGES.audioTooLarge, diagnosticLog: false }
  }
  if (info.status === 415) {
    return { kind: 'discard', message: VOICE_MESSAGES.unsupportedFormat, diagnosticLog: true }
  }
  if (info.status === 422) {
    return { kind: 'discard', message: VOICE_MESSAGES.audioInvalid, diagnosticLog: false }
  }
  if (info.status === 504) {
    return { kind: 'transient', message: VOICE_MESSAGES.transcriptionTimeout, diagnosticLog: false }
  }
  if (info.status === 502) {
    return { kind: 'transient', message: VOICE_MESSAGES.transcriptionFailed, diagnosticLog: false }
  }
  if (info.status !== undefined && info.status >= 500) {
    return { kind: 'transient', message: VOICE_MESSAGES.transcriptionFailed, diagnosticLog: false }
  }
  return { kind: 'transient', message: VOICE_MESSAGES.offline, diagnosticLog: false }
}

export type SubmitFailureMapping =
  | { kind: 'consumed' }
  | { kind: 'expired' }
  | { kind: 'mismatch'; message: string }
  | { kind: 'retryable'; message: string }

/**
 * Map a submit failure per M7b spec §4.8: 409 (including already-consumed)
 * is idempotent success; 410 requires re-recording; 422 is a permanent
 * mismatch (diagnostic log + generic toast); everything else keeps the
 * confirmed draft in the draft store for an explicit submit retry.
 */
export function classifySubmitError(error: unknown): SubmitFailureMapping {
  const info = voiceErrorInfo(error)
  if (info.status === 409) return { kind: 'consumed' }
  if (info.status === 410) return { kind: 'expired' }
  if (info.status === 422) return { kind: 'mismatch', message: VOICE_MESSAGES.submitFailed }
  const message =
    info.kind === 'offline' || info.kind === 'aborted' || info.status === undefined
      ? VOICE_MESSAGES.offline
      : VOICE_MESSAGES.submitFailed
  return { kind: 'retryable', message }
}

export type ConfirmFailureMapping = 'expired' | 'unusable' | 'retryable'

/** Confirm endpoint errors: B returns 404/409/410/403 (M7b spec §3.1). */
export function classifyConfirmError(error: unknown): ConfirmFailureMapping {
  const info = voiceErrorInfo(error)
  if (info.status === 410) return 'expired'
  if (info.status === 403 || info.status === 404 || info.status === 409) return 'unusable'
  return 'retryable'
}

// ---------------------------------------------------------------------------
// Public state
// ---------------------------------------------------------------------------

export interface UploadFailureState {
  /** `transient` keeps the blob for a same-audio retry; `discard` requires re-recording. */
  kind: 'transient' | 'discard'
  message: string
  retryable: boolean
}

export interface DraftSheetState {
  draftId: string
  text: string
  provider: string
  region: string
  /** Optional disclosure fields from the upload response (not persisted). */
  language: string | null
  durationSeconds: number | null
  expiresAt: string
  /** Confirm/submit in flight. */
  submitting: boolean
  /** Confirm succeeded but submit failed — `retrySubmit` is the only path. */
  submitFailed: boolean
}

export type VoiceDisableReason = 'provider_unavailable' | 'capability_denied'

export type VoiceDraftState =
  | { status: 'idle' }
  | { status: 'arming' }
  | { status: 'recording'; durationSeconds: number; cancelPending: boolean }
  | { status: 'stopping' }
  | { status: 'uploading'; elapsedSeconds: number }
  | { status: 'draft'; draft: DraftSheetState }
  | { status: 'failure'; failure: UploadFailureState }
  | { status: 'cancelled' }
  | { status: 'tooShort' }
  | { status: 'denied' }
  | { status: 'unavailable' }
  | { status: 'disabled'; reason: VoiceDisableReason }
  | { status: 'abortedByUser' }

// ---------------------------------------------------------------------------
// Draft store payload (M7b spec §4.7.4)
// ---------------------------------------------------------------------------

export interface StoredVoiceDraft {
  draftId: string
  text: string
  bindingId: string
  conversationId: string
  provider: string
  region: string
  expiresAt: string
}

function parseStoredVoiceDraft(raw: string): StoredVoiceDraft | null {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof value !== 'object' || value === null) return null
  const candidate = value as Record<string, unknown>
  if (
    typeof candidate.draftId !== 'string' ||
    typeof candidate.text !== 'string' ||
    typeof candidate.bindingId !== 'string' ||
    typeof candidate.conversationId !== 'string' ||
    typeof candidate.provider !== 'string' ||
    typeof candidate.region !== 'string' ||
    typeof candidate.expiresAt !== 'string'
  ) {
    return null
  }
  return {
    draftId: candidate.draftId,
    text: candidate.text,
    bindingId: candidate.bindingId,
    conversationId: candidate.conversationId,
    provider: candidate.provider,
    region: candidate.region,
    expiresAt: candidate.expiresAt,
  }
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

export interface VoiceDraftControllerOptions {
  /** Fresh recorder per press — tracks are released on stop/abort (M7b spec §4.3.2). */
  createRecorder: () => AudioRecorder
  uploader: AudioUploader
  draftApi: VoiceDraftApi
  submit: SubmitMessage
  clock: VoiceClock
  scheduler: VoiceScheduler
  storage: VoiceStorage
  logger: VoiceLogger
  bindingId: string
  conversationId: string
  /**
   * B side `speech_to_text_enabled` capability flag (§4.9). Injected as a
   * parameter because the flag has not landed in generated contracts yet;
   * while false, `press()` is a no-op and the UI does not render the button.
   */
  speechToTextEnabled?: boolean
  callbacks: VoiceDraftCallbacks
}

export interface VoiceDraftControllerLike {
  getState(): VoiceDraftState
  /** pointerdown / keydown(Space|Enter) */
  press(): void
  /** pointerup / keyup */
  release(): void
  /** pointermove slide distance from the press origin (px) */
  slide(distancePx: number): void
  /** pointercancel / visibilitychange(hidden) while recording */
  cancel(): void
  /** User abandons an in-flight upload (M7b spec §4.4 abortedByUser). */
  cancelUpload(): void
  /** Re-upload the retained blob after a transient failure (M7b spec §4.6). */
  retryUpload(): void
  /** Drop everything after a discard failure — re-record from scratch. */
  reRecord(): void
  /** Draft sheet [确认发送] with the (possibly edited) text. */
  confirm(text: string): void
  /** Retry only the submit after confirm(204) succeeded (M7b spec §4.8). */
  retrySubmit(): void
  /** Draft sheet [放弃] — best-effort cancel, never submits. */
  discard(): void
  /** Reload draft chip/sheet from the draft store (M7b spec §4.7.4). */
  restore(): void
}

export class VoiceDraftController implements VoiceDraftControllerLike {
  private state: VoiceDraftState = { status: 'idle' }
  private recorder: AudioRecorder | null = null
  private recordedAudio: RecordedAudio | null = null
  private startedAtMs = 0
  private uploadStartedAtMs = 0
  private autoStopTimer: unknown | null = null
  private uploadAbort: AbortController | null = null
  private uploadAbortReason: 'user' | 'timeout' | null = null
  private uploadTimeoutTimer: unknown | null = null
  private cancelPending = false
  private abandonedDuringArming = false
  private lastSubmitText: string | null = null
  private readonly speechToTextEnabled: boolean

  constructor(private readonly options: VoiceDraftControllerOptions) {
    this.speechToTextEnabled = options.speechToTextEnabled ?? true
  }

  private get clock(): VoiceClock {
    return this.options.clock
  }

  private get scheduler(): VoiceScheduler {
    return this.options.scheduler
  }

  private get storage(): VoiceStorage {
    return this.options.storage
  }

  private get callbacks(): VoiceDraftCallbacks {
    return this.options.callbacks
  }

  // -------------------------------------------------------------------------
  // State access
  // -------------------------------------------------------------------------

  getState(): VoiceDraftState {
    if (this.state.status === 'recording') {
      return { ...this.state, durationSeconds: this.recordingDurationSeconds() }
    }
    if (this.state.status === 'uploading') {
      return { ...this.state, elapsedSeconds: this.uploadElapsedSeconds() }
    }
    return this.state
  }

  private transition(state: VoiceDraftState): void {
    this.state = state
    this.callbacks.onState(this.getState())
  }

  private toast(message: string): void {
    this.callbacks.onToast({ message })
  }

  private recordingDurationSeconds(): number {
    return Math.max(0, (this.clock.now() - this.startedAtMs) / 1000)
  }

  private uploadElapsedSeconds(): number {
    return Math.max(0, (this.clock.now() - this.uploadStartedAtMs) / 1000)
  }

  // -------------------------------------------------------------------------
  // Press / hold (M7b spec §4.4)
  // -------------------------------------------------------------------------

  press(): void {
    if (!this.speechToTextEnabled) return
    const status = this.state.status
    // Re-pressable only from terminal-but-benign states; explicit actions are
    // required from draft/failure, and disabled/unavailable end the session.
    if (
      status === 'arming' ||
      status === 'recording' ||
      status === 'stopping' ||
      status === 'uploading' ||
      status === 'draft' ||
      status === 'failure' ||
      status === 'disabled' ||
      status === 'unavailable'
    ) {
      return
    }
    this.abandonedDuringArming = false
    this.transition({ status: 'arming' })
    const recorder = this.options.createRecorder()
    this.recorder = recorder
    void recorder.start().then(
      () => {
        if (this.state.status !== 'arming' || this.recorder !== recorder) return
        if (this.abandonedDuringArming) {
          // Released before start() completed — abandon, never record.
          this.recorder = null
          recorder.abort()
          this.transition({ status: 'idle' })
          return
        }
        this.beginRecording()
      },
      (error: unknown) => {
        if (this.state.status !== 'arming' || this.recorder !== recorder) return
        this.recorder = null
        if (error instanceof PermissionDeniedError) {
          this.transition({ status: 'denied' })
          this.toast(VOICE_MESSAGES.permissionDenied)
        } else {
          this.transition({ status: 'unavailable' })
          this.toast(VOICE_MESSAGES.recorderUnavailable)
        }
      },
    )
  }

  release(): void {
    if (this.state.status === 'arming') {
      this.abandonedDuringArming = true
      return
    }
    if (this.state.status !== 'recording') return
    this.clearAutoStopTimer()
    if (this.cancelPending) {
      this.finishCancelled()
      return
    }
    this.finishRecording(this.recordingDurationSeconds())
  }

  slide(distancePx: number): void {
    if (this.state.status !== 'recording') return
    let next = this.cancelPending
    if (distancePx > CANCEL_SLIDE_THRESHOLD_PX) next = true
    else if (distancePx < CANCEL_RESUME_THRESHOLD_PX) next = false
    // Inside the [48, 96] band the previous value holds (hysteresis).
    if (next === this.cancelPending) return
    this.cancelPending = next
    this.transition({ ...this.state, cancelPending: next })
  }

  cancel(): void {
    if (this.state.status === 'arming') {
      this.abandonedDuringArming = true
      return
    }
    if (this.state.status !== 'recording') return
    this.clearAutoStopTimer()
    this.finishCancelled()
  }

  private beginRecording(): void {
    this.startedAtMs = this.clock.now()
    this.cancelPending = false
    this.transition({ status: 'recording', durationSeconds: 0, cancelPending: false })
    this.autoStopTimer = this.scheduler.set(() => {
      if (this.state.status !== 'recording') return
      this.clearAutoStopTimer()
      this.finishRecording(MAX_RECORDING_SECONDS)
    }, MAX_RECORDING_SECONDS * 1000)
  }

  private clearAutoStopTimer(): void {
    if (this.autoStopTimer === null) return
    this.scheduler.clear(this.autoStopTimer)
    this.autoStopTimer = null
  }

  private finishCancelled(): void {
    const recorder = this.recorder
    this.recorder = null
    this.recordedAudio = null
    this.cancelPending = false
    recorder?.abort()
    this.transition({ status: 'cancelled' })
  }

  private finishRecording(durationSeconds: number): void {
    const recorder = this.recorder
    this.recorder = null
    this.transition({ status: 'stopping' })
    if (durationSeconds < MIN_RECORDING_SECONDS) {
      recorder?.abort()
      this.transition({ status: 'tooShort' })
      this.toast(VOICE_MESSAGES.tooShort)
      return
    }
    if (recorder === null) {
      this.failUpload({ kind: 'discard', message: VOICE_MESSAGES.audioInvalid, retryable: false })
      return
    }
    void recorder.stop().then(
      (audio) => this.beginUpload(audio),
      () => {
        // Capture pipeline failed after recording started — nothing to retry.
        this.failUpload({ kind: 'discard', message: VOICE_MESSAGES.audioInvalid, retryable: false })
      },
    )
  }

  // -------------------------------------------------------------------------
  // Upload (M7b spec §4.5/§4.6)
  // -------------------------------------------------------------------------

  private beginUpload(audio: RecordedAudio): void {
    if (!ALLOWED_AUDIO_MIME_TYPES.includes(audio.mimeType)) {
      this.options.logger.error('voice upload rejected: unsupported mime type', { mimeType: audio.mimeType })
      this.failUpload({ kind: 'discard', message: VOICE_MESSAGES.unsupportedFormat, retryable: false })
      return
    }
    if (audio.blob.size <= 0 || audio.blob.size > MAX_AUDIO_BYTES) {
      this.options.logger.error('voice upload rejected: blob size out of bounds', { bytes: audio.blob.size })
      this.failUpload({ kind: 'discard', message: VOICE_MESSAGES.audioTooLarge, retryable: false })
      return
    }
    this.recordedAudio = audio
    this.uploadStartedAtMs = this.clock.now()
    this.transition({ status: 'uploading', elapsedSeconds: 0 })
    void this.performUpload()
  }

  private async performUpload(): Promise<void> {
    const audio = this.recordedAudio
    if (audio === null) return
    const controller = new AbortController()
    this.uploadAbort = controller
    this.uploadAbortReason = null
    this.uploadTimeoutTimer = this.scheduler.set(() => {
      if (this.uploadAbort !== controller) return
      this.uploadAbortReason = 'timeout'
      controller.abort()
    }, UPLOAD_TIMEOUT_MS)
    try {
      const response = await this.options.uploader(audio, {
        bindingId: this.options.bindingId,
        conversationId: this.options.conversationId,
        signal: controller.signal,
      })
      if (this.state.status !== 'uploading' || this.uploadAbort !== controller) return
      this.settleUpload()
      // Upload settled — release the raw audio; it must never outlive this.
      this.recordedAudio = null
      this.enterDraft(response)
    } catch (error) {
      if (this.state.status !== 'uploading' || this.uploadAbort !== controller) return
      // Capture the abort reason before settleUpload() clears it.
      const abortReason = this.uploadAbortReason
      this.settleUpload()
      if (abortReason === 'user') {
        this.recordedAudio = null
        this.transition({ status: 'abortedByUser' })
        return
      }
      if (abortReason === 'timeout') {
        // Keep the blob: the same audio can be retried (M7b spec §4.6).
        this.failUpload({ kind: 'transient', message: VOICE_MESSAGES.offline, retryable: true })
        return
      }
      const mapping = classifyUploadError(error)
      if (mapping.kind === 'provider_unavailable' || mapping.kind === 'capability_denied') {
        this.recordedAudio = null
        if (mapping.diagnosticLog) this.logUploadDiagnostic(error)
        this.transition({
          status: 'disabled',
          reason: mapping.kind === 'provider_unavailable' ? 'provider_unavailable' : 'capability_denied',
        })
        this.toast(mapping.message)
        return
      }
      if (mapping.diagnosticLog) this.logUploadDiagnostic(error)
      // 5xx/offline keep the blob (retryable); 4xx discard it (re-record only).
      this.failUpload({ kind: mapping.kind, message: mapping.message, retryable: mapping.kind === 'transient' })
    }
  }

  private logUploadDiagnostic(error: unknown): void {
    const info = voiceErrorInfo(error)
    const details: Record<string, unknown> = {}
    if (info.status !== undefined) details.status = info.status
    if (info.code !== undefined) details.code = info.code
    if (info.kind !== undefined) details.kind = info.kind
    this.options.logger.error('voice upload failed', details)
  }

  private failUpload(failure: UploadFailureState): void {
    if (failure.kind === 'discard') this.recordedAudio = null
    this.transition({ status: 'failure', failure })
    this.toast(failure.message)
  }

  private settleUpload(): void {
    this.uploadAbort = null
    this.uploadAbortReason = null
    if (this.uploadTimeoutTimer !== null) {
      this.scheduler.clear(this.uploadTimeoutTimer)
      this.uploadTimeoutTimer = null
    }
  }

  cancelUpload(): void {
    if (this.state.status !== 'uploading' || this.uploadAbort === null) return
    if (this.uploadAbortReason === null) this.uploadAbortReason = 'user'
    this.uploadAbort.abort()
  }

  retryUpload(): void {
    if (this.state.status !== 'failure') return
    if (!this.state.failure.retryable || this.recordedAudio === null) return
    this.uploadStartedAtMs = this.clock.now()
    this.transition({ status: 'uploading', elapsedSeconds: 0 })
    void this.performUpload()
  }

  reRecord(): void {
    if (this.state.status !== 'failure') return
    this.recordedAudio = null
    this.transition({ status: 'idle' })
  }

  // -------------------------------------------------------------------------
  // Draft sheet (M7b spec §4.7/§4.8)
  // -------------------------------------------------------------------------

  private enterDraft(response: TranscriptionUploadResponse): void {
    const draft: DraftSheetState = {
      draftId: response.draft_id,
      text: response.transcript,
      provider: response.provider,
      region: response.region,
      language: response.language,
      durationSeconds: response.duration_seconds,
      expiresAt: response.expires_at,
      submitting: false,
      submitFailed: false,
    }
    this.transition({ status: 'draft', draft })
    this.persistDraft(draft.text)
  }

  private persistDraft(text: string): void {
    if (this.state.status !== 'draft') return
    const stored: StoredVoiceDraft = {
      draftId: this.state.draft.draftId,
      text,
      bindingId: this.options.bindingId,
      conversationId: this.options.conversationId,
      provider: this.state.draft.provider,
      region: this.state.draft.region,
      expiresAt: this.state.draft.expiresAt,
    }
    this.storage.setItem(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored))
  }

  private clearStoredDraft(): void {
    this.storage.removeItem(VOICE_DRAFT_STORAGE_KEY)
  }

  confirm(text: string): void {
    if (this.state.status !== 'draft' || this.state.draft.submitting) return
    const draftId = this.state.draft.draftId
    this.lastSubmitText = text
    // Keep the draft store in sync with the edited text so a submit retry
    // after an app kill still carries the user's final wording.
    this.transition({
      status: 'draft',
      draft: { ...this.state.draft, text, submitting: true, submitFailed: false },
    })
    this.persistDraft(text)
    void this.options.draftApi.confirm(draftId).then(
      () => this.submitDraft(draftId, text),
      (error: unknown) => this.handleConfirmFailure(error),
    )
  }

  private async submitDraft(draftId: string, text: string): Promise<void> {
    try {
      await this.options.submit({
        conversationId: this.options.conversationId,
        text,
        draftRef: draftId,
      })
    } catch (error) {
      this.handleSubmitFailure(error)
      return
    }
    this.finishSubmitted()
  }

  private handleConfirmFailure(error: unknown): void {
    const kind = classifyConfirmError(error)
    if (kind === 'retryable') {
      if (this.state.status !== 'draft') return
      this.transition({ status: 'draft', draft: { ...this.state.draft, submitting: false } })
      this.toast(VOICE_MESSAGES.offline)
      return
    }
    // expired / unusable: nothing left to confirm — close and re-record.
    this.clearStoredDraft()
    this.lastSubmitText = null
    this.transition({ status: 'idle' })
    this.toast(kind === 'expired' ? VOICE_MESSAGES.draftExpired : VOICE_MESSAGES.confirmFailed)
  }

  private handleSubmitFailure(error: unknown): void {
    const mapping = classifySubmitError(error)
    if (mapping.kind === 'consumed') {
      // Idempotent success — the B side CAS consumed the draft exactly once.
      this.finishSubmitted()
      return
    }
    if (mapping.kind === 'expired') {
      this.clearStoredDraft()
      this.lastSubmitText = null
      this.transition({ status: 'idle' })
      this.toast(VOICE_MESSAGES.draftExpired)
      return
    }
    if (this.state.status !== 'draft') return
    if (mapping.kind === 'mismatch') {
      this.options.logger.error('voice submit rejected: draft conversation mismatch', {
        draftId: this.state.draft.draftId,
        conversationId: this.options.conversationId,
      })
    }
    // Keep the confirmed draft in the draft store; retry re-submits only.
    this.transition({ status: 'draft', draft: { ...this.state.draft, submitting: false, submitFailed: true } })
    this.toast(mapping.message)
  }

  private finishSubmitted(): void {
    this.clearStoredDraft()
    this.lastSubmitText = null
    this.transition({ status: 'idle' })
    this.toast(VOICE_MESSAGES.submitted)
  }

  retrySubmit(): void {
    if (this.state.status !== 'draft' || !this.state.draft.submitFailed || this.state.draft.submitting) return
    const draftId = this.state.draft.draftId
    const text = this.lastSubmitText ?? this.state.draft.text
    this.lastSubmitText = text
    this.transition({ status: 'draft', draft: { ...this.state.draft, submitting: true, submitFailed: false } })
    void this.submitDraft(draftId, text)
  }

  discard(): void {
    if (this.state.status !== 'draft' || this.state.draft.submitting) return
    const draftId = this.state.draft.draftId
    this.clearStoredDraft()
    this.lastSubmitText = null
    this.transition({ status: 'idle' })
    // Best-effort: 409/410/404/network failures are silently ignored — the
    // server-side 1h TTL is the backstop (M7b spec §4.7.1).
    void this.options.draftApi.cancel(draftId).catch(() => {})
  }

  // -------------------------------------------------------------------------
  // Session recovery (M7b spec §4.7.4)
  // -------------------------------------------------------------------------

  restore(): void {
    if (this.state.status !== 'idle') return
    const raw = this.storage.getItem(VOICE_DRAFT_STORAGE_KEY)
    if (raw === null) return
    const parsed = parseStoredVoiceDraft(raw)
    if (parsed === null) {
      this.storage.removeItem(VOICE_DRAFT_STORAGE_KEY)
      return
    }
    if (parsed.conversationId !== this.options.conversationId) {
      // Another conversation's draft — leave it in place for that composer.
      return
    }
    // Treat malformed timestamps as expired: the draft store is user-writable
    // and must never resurrect an unexpirable draft (M7b spec §4.7.4).
    const expiresAtMs = Date.parse(parsed.expiresAt)
    if (Number.isNaN(expiresAtMs) || this.clock.now() >= expiresAtMs) {
      this.storage.removeItem(VOICE_DRAFT_STORAGE_KEY)
      this.toast(VOICE_MESSAGES.draftExpired)
      return
    }
    const draft: DraftSheetState = {
      draftId: parsed.draftId,
      text: parsed.text,
      provider: parsed.provider,
      region: parsed.region,
      language: null,
      durationSeconds: null,
      expiresAt: parsed.expiresAt,
      submitting: false,
      submitFailed: false,
    }
    this.transition({ status: 'draft', draft })
  }
}
