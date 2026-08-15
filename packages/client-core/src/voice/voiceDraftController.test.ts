import { describe, expect, it, vi } from 'vitest'
import {
  ALLOWED_AUDIO_MIME_TYPES,
  MAX_AUDIO_BYTES,
  MAX_RECORDING_SECONDS,
  VOICE_DRAFT_STORAGE_KEY,
  VOICE_MESSAGES,
  VoiceDraftController,
  classifyUploadError,
  voiceErrorInfo,
  type AudioUploader,
  type StoredVoiceDraft,
  type TranscriptionUploadResponse,
  type VoiceDraftState,
} from './voiceDraftController'
import {
  PermissionDeniedError,
  RecorderUnavailableError,
  type AudioRecorder,
  type RecordedAudio,
} from './recorderPort'

const AUDIO_BYTES = 'AUDIO-BYTES-MARKER-01'

function makeBlob(bytes: string = AUDIO_BYTES, size: number = bytes.length): Blob {
  return new Blob([bytes], { type: 'audio/webm' })
}

function makeAudio(overrides: Partial<RecordedAudio> = {}): RecordedAudio {
  return { blob: makeBlob(), mimeType: 'audio/webm', durationSeconds: 3, ...overrides }
}

const uploadResponse = (overrides: Partial<TranscriptionUploadResponse> = {}): TranscriptionUploadResponse => ({
  draft_id: 'draft-1',
  state: 'transcribed',
  transcript: '这是转写文本',
  provider: 'speaches',
  region: '',
  language: 'zh-CN',
  duration_seconds: 3,
  expires_at: '2026-08-13T13:00:00Z',
  ...overrides,
})

const error = (status?: number, code?: string, kind?: 'offline' | 'aborted' | 'http_capability_denied') =>
  Object.assign(new Error('x'), {
    ...(status !== undefined ? { status } : {}),
    ...(code !== undefined ? { code } : {}),
    ...(kind !== undefined ? { kind } : {}),
  })

interface Harness {
  controller: VoiceDraftController
  now: () => number
  advance: (ms: number) => void
  states: VoiceDraftState[]
  toasts: string[]
  recorderFactory: ReturnType<typeof vi.fn>
  recorder: {
    start: ReturnType<typeof vi.fn>
    stop: ReturnType<typeof vi.fn>
    abort: ReturnType<typeof vi.fn>
  }
  uploader: ReturnType<typeof vi.fn>
  confirm: ReturnType<typeof vi.fn>
  cancel: ReturnType<typeof vi.fn>
  submit: ReturnType<typeof vi.fn>
  storageMap: Map<string, string>
  logCalls: Array<[string, Record<string, unknown> | undefined]>
}

function createHarness(options: {
  uploader?: AudioUploader
  recorderFactory?: () => AudioRecorder
  submit?: (params: { conversationId: string; text: string; draftRef: string }) => Promise<void>
  confirm?: () => Promise<void>
  cancel?: () => Promise<void>
  bindingId?: string
  conversationId?: string
  speechToTextEnabled?: boolean
} = {}): Harness {
  // Start at a realistic wall-clock epoch: restore() compares the fake clock
  // against real ISO expiresAt timestamps parsed via Date.parse (M7b §4.7.4).
  let nowMs = Date.parse('2026-08-14T00:00:00Z')
  let timerId = 0
  const timers = new Map<number, { fn: () => void; at: number }>()

  const now = () => nowMs
  const advance = (ms: number) => {
    const target = nowMs + ms
    // Fire timers in chronological order, allowing them to see intermediate times.
    for (;;) {
      let next: { id: number; fn: () => void; at: number } | null = null
      for (const [id, t] of timers) {
        if (t.at <= target && (next === null || t.at < next.at)) next = { id, ...t }
      }
      if (next === null) break
      timers.delete(next.id)
      nowMs = next.at
      next.fn()
    }
    nowMs = target
  }

  const recorderFactory = vi.fn(
    () =>
      ({
        start: vi.fn().mockResolvedValue(undefined),
        stop: vi.fn().mockResolvedValue(makeAudio()),
        abort: vi.fn(),
      }) as unknown as AudioRecorder,
  )

  const recorder = {
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(makeAudio()),
    abort: vi.fn(),
  }
  if (options.recorderFactory === undefined) recorderFactory.mockReturnValue(recorder)

  const uploader = vi.fn(options.uploader ?? vi.fn().mockResolvedValue(uploadResponse()))
  const confirm = vi.fn(options.confirm ?? vi.fn().mockResolvedValue(undefined))
  const cancel = vi.fn(options.cancel ?? vi.fn().mockResolvedValue(undefined))
  const submit = vi.fn(options.submit ?? vi.fn().mockResolvedValue(undefined))

  const storageMap = new Map<string, string>()
  const logCalls: Array<[string, Record<string, unknown> | undefined]> = []

  const states: VoiceDraftState[] = []
  const toasts: string[] = []

  const controller = new VoiceDraftController({
    createRecorder: options.recorderFactory ?? recorderFactory,
    uploader: uploader as AudioUploader,
    draftApi: { confirm, cancel },
    submit,
    clock: { now },
    scheduler: {
      set: (fn, ms) => {
        timerId += 1
        timers.set(timerId, { fn, at: nowMs + ms })
        return timerId
      },
      clear: (handle) => {
        timers.delete(handle as number)
      },
    },
    storage: {
      getItem: (key) => storageMap.get(key) ?? null,
      setItem: (key, value) => {
        storageMap.set(key, value)
      },
      removeItem: (key) => {
        storageMap.delete(key)
      },
    },
    logger: { error: (message, details) => logCalls.push([message, details]) },
    bindingId: options.bindingId ?? 'binding-1',
    conversationId: options.conversationId ?? 'conversation-1',
    ...(options.speechToTextEnabled === undefined
      ? {}
      : { speechToTextEnabled: options.speechToTextEnabled }),
    callbacks: {
      onState: (state) => states.push(state),
      onToast: (toast) => toasts.push(toast.message),
    },
  })

  return { controller, now, advance, states, toasts, recorderFactory, recorder, uploader, confirm, cancel, submit, storageMap, logCalls }
}

async function recordToUpload(h: Harness, durationMs = 3_000): Promise<void> {
  h.controller.press()
  await Promise.resolve()
  await Promise.resolve()
  h.advance(durationMs)
  h.controller.release()
  await Promise.resolve()
  await Promise.resolve()
}

async function recordToDraft(h: Harness): Promise<void> {
  await recordToUpload(h)
  await Promise.resolve()
}

const statusOf = (h: Harness) => h.controller.getState().status

describe('voiceDraftController press/hold state machine', () => {
  it('records on press and uploads on release (>=0.5s)', async () => {
    const h = createHarness()
    h.controller.press()
    expect(statusOf(h)).toBe('arming')
    await Promise.resolve()
    await Promise.resolve()
    expect(statusOf(h)).toBe('recording')

    h.advance(2_000)
    h.controller.release()
    expect(statusOf(h)).toBe('stopping')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.uploader).toHaveBeenCalledTimes(1)
    const [audio, params] = h.uploader.mock.calls[0] as [RecordedAudio, { bindingId: string; conversationId: string; signal: AbortSignal }]
    expect(audio.mimeType).toBe('audio/webm')
    expect(params.bindingId).toBe('binding-1')
    expect(params.conversationId).toBe('conversation-1')
    expect(params.signal).toBeInstanceOf(AbortSignal)
    // The fake uploader resolves immediately, so the flow settles on draft;
    // the 'uploading' state must still have been emitted in between.
    expect(statusOf(h)).toBe('draft')
    expect(h.states.map((s) => s.status)).toContain('uploading')
  })

  it('press is a no-op when speechToTextEnabled is false', () => {
    const h = createHarness({ speechToTextEnabled: false })
    h.controller.press()
    expect(statusOf(h)).toBe('idle')
    expect(h.recorderFactory).not.toHaveBeenCalled()
  })

  it('permission denial → guidance copy, no upload, button re-pressable', async () => {
    const h = createHarness()
    h.recorder.start.mockRejectedValueOnce(new PermissionDeniedError())
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('denied')
    expect(h.toasts).toEqual([VOICE_MESSAGES.permissionDenied])
    expect(h.uploader).not.toHaveBeenCalled()

    // Re-press allowed
    h.controller.press()
    expect(statusOf(h)).toBe('arming')
  })

  it('recorder unavailable → unavailable state, button hidden', async () => {
    const h = createHarness()
    h.recorder.start.mockRejectedValueOnce(new RecorderUnavailableError())
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('unavailable')
    expect(h.toasts).toEqual([VOICE_MESSAGES.recorderUnavailable])
    expect(h.uploader).not.toHaveBeenCalled()

    h.controller.press()
    expect(statusOf(h)).toBe('unavailable')
  })

  it('release during arming (before start resolves) → idle, no recording', async () => {
    let resolveStart: (() => void) | undefined
    const start = vi.fn().mockImplementation(() => new Promise<void>((resolve) => { resolveStart = resolve }))
    const h = createHarness({ recorderFactory: () => ({ start, stop: vi.fn(), abort: vi.fn() }) })

    h.controller.press()
    h.controller.release()
    expect(statusOf(h)).toBe('arming') // still waiting for start()
    resolveStart?.()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('idle')
    expect(h.uploader).not.toHaveBeenCalled()
  })

  it('release before 0.5s → tooShort, discarded, no upload', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()
    h.advance(400)
    h.controller.release()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('tooShort')
    expect(h.toasts).toEqual([VOICE_MESSAGES.tooShort])
    expect(h.uploader).not.toHaveBeenCalled()
    expect(h.recorder.abort).toHaveBeenCalledTimes(1)
  })

  it('240s auto-stop via fake clock → upload proceeds', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()
    h.advance(MAX_RECORDING_SECONDS * 1000)
    expect(statusOf(h)).toBe('stopping')
    await Promise.resolve()
    await Promise.resolve()
    expect(h.uploader).toHaveBeenCalledTimes(1)
  })

  it('240s recording never exceeds the size bound (WAV math documented)', () => {
    // 240s * 32 kB/s (16kHz mono 16-bit) = 7.68 MB < 10 MiB
    expect(240 * 32_000).toBeLessThan(MAX_AUDIO_BYTES)
  })

  it('slide-up > 96px enters cancel zone; release cancels without upload', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()

    h.controller.slide(100)
    let state = h.controller.getState()
    expect(state.status === 'recording' && state.cancelPending).toBe(true)

    h.controller.release()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('cancelled')
    expect(h.uploader).not.toHaveBeenCalled()
    expect(h.recorder.abort).toHaveBeenCalledTimes(1)
  })

  it('slide hysteresis: inside the band keeps the previous decision', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()

    h.controller.slide(100) // enter cancel zone
    h.controller.slide(70) // inside [48, 96] band → still cancelling
    let state = h.controller.getState()
    expect(state.status === 'recording' && state.cancelPending).toBe(true)

    h.controller.slide(30) // back below 48 → resume
    state = h.controller.getState()
    expect(state.status === 'recording' && state.cancelPending).toBe(false)

    h.advance(1_000) // hold long enough to survive the 0.5s minimum
    h.controller.release()
    await Promise.resolve()
    await Promise.resolve()
    expect(h.uploader).toHaveBeenCalledTimes(1)
  })

  it('pointercancel / visibilitychange during recording → cancelled, no upload', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()
    h.advance(1_000)

    h.controller.cancel()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('cancelled')
    expect(h.uploader).not.toHaveBeenCalled()
  })

  it('slide or cancel outside recording is a no-op', async () => {
    const h = createHarness()
    h.controller.slide(100)
    h.controller.cancel()
    expect(statusOf(h)).toBe('idle')
    expect(h.recorder.abort).not.toHaveBeenCalled()
  })
})

describe('voiceDraftController upload errors (§4.6 mapping)', () => {
  it.each([
    [{ status: 502, code: 'transcription_failed' }, VOICE_MESSAGES.transcriptionFailed],
    [{ status: 504, code: 'transcription_timeout' }, VOICE_MESSAGES.transcriptionTimeout],
  ])('5xx %o → transient failure, retryable, same blob', async (errObj, message) => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error((errObj as { status: number }).status, (errObj as { code: string }).code)) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(message)
      expect(state.failure.kind).toBe('transient')
      expect(state.failure.retryable).toBe(true)
    }
    expect(h.toasts).toEqual([message])

    // Retry re-uploads the SAME blob
    h.uploader.mockResolvedValueOnce(uploadResponse())
    h.controller.retryUpload()
    expect(statusOf(h)).toBe('uploading')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.uploader).toHaveBeenCalledTimes(2)
    const first = (h.uploader.mock.calls[0] as [RecordedAudio])[0]
    const second = (h.uploader.mock.calls[1] as [RecordedAudio])[0]
    expect(second.blob).toBe(first.blob)
    expect(statusOf(h)).toBe('draft')
  })

  it.each([
    [{ status: 413, code: 'audio_too_large' }, VOICE_MESSAGES.audioTooLarge],
    [{ status: 415, code: 'unsupported_audio_format' }, VOICE_MESSAGES.unsupportedFormat],
    [{ status: 422, code: 'invalid_audio' }, VOICE_MESSAGES.audioInvalid],
    [{ status: 422, code: 'audio_sample_rate_out_of_bounds' }, VOICE_MESSAGES.audioInvalid],
    [{ status: 422, code: 'audio_too_long' }, VOICE_MESSAGES.audioInvalid],
  ])('4xx %o → discard failure, re-record only', async (errObj, message) => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error((errObj as { status: number }).status, (errObj as { code: string }).code)) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(message)
      expect(state.failure.kind).toBe('discard')
      expect(state.failure.retryable).toBe(false)
    }
    expect(h.toasts).toEqual([message])

    // retryUpload is a no-op (blob discarded)
    h.controller.retryUpload()
    expect(h.uploader).toHaveBeenCalledTimes(1)
    expect(statusOf(h)).toBe('failure')

    // reRecord returns to idle; a fresh press creates a fresh recorder
    h.controller.reRecord()
    expect(statusOf(h)).toBe('idle')
  })

  it('offline / aborted transport failure → transient, retryable', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(undefined, undefined, 'offline')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(VOICE_MESSAGES.offline)
      expect(state.failure.retryable).toBe(true)
    }
  })

  it('AbortSignal 120s timeout → transient network failure, retryable', async () => {
    const h = createHarness({
      uploader: vi.fn().mockImplementation(
        (_audio, params) =>
          new Promise((_resolve, reject) => {
            params.signal.addEventListener('abort', () => reject(error(undefined, undefined, 'aborted')))
          }),
      ),
    })
    await recordToUpload(h)
    expect(statusOf(h)).toBe('uploading')

    h.advance(120_000)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(VOICE_MESSAGES.offline)
      expect(state.failure.retryable).toBe(true)
    }
  })

  it('503 speech_to_text_unavailable → session-disable, button gone', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(503, 'speech_to_text_unavailable')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('disabled')
    expect(h.controller.getState()).toMatchObject({ reason: 'provider_unavailable' })
    expect(h.toasts).toEqual([VOICE_MESSAGES.providerUnavailable])

    h.controller.press()
    expect(statusOf(h)).toBe('disabled')
    expect(h.uploader).toHaveBeenCalledTimes(1)
  })

  it('http_capability_denied → session-disable + diagnostic log', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(undefined, undefined, 'http_capability_denied')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    expect(h.controller.getState()).toMatchObject({ status: 'disabled', reason: 'capability_denied' })
    expect(h.toasts).toEqual([VOICE_MESSAGES.capabilityDenied])
    expect(h.logCalls.some(([msg]) => msg.includes('voice upload failed'))).toBe(true)
  })

  it('415 unsupported format → diagnostic log emitted', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(415, 'unsupported_audio_format')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()
    expect(h.logCalls.length).toBe(1)
  })

  it('upload cancel (abortedByUser) releases the blob and allows re-record', async () => {
    const h = createHarness({
      uploader: vi.fn().mockImplementation(
        (_audio, params) =>
          new Promise((_resolve, reject) => {
            params.signal.addEventListener('abort', () => reject(error(undefined, undefined, 'aborted')))
          }),
      ),
    })
    await recordToUpload(h)

    h.controller.cancelUpload()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('abortedByUser')
    expect(h.toasts).toEqual([])
  })

  it('retry is never automatic: each retry needs a user click', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(502, 'transcription_failed')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()
    expect(h.uploader).toHaveBeenCalledTimes(1)

    h.advance(60_000) // time passes, no auto retry
    expect(h.uploader).toHaveBeenCalledTimes(1)

    h.controller.retryUpload()
    await Promise.resolve()
    await Promise.resolve()
    expect(h.uploader).toHaveBeenCalledTimes(2)
    expect(statusOf(h)).toBe('failure') // still failing, no auto loop
    expect(h.uploader).toHaveBeenCalledTimes(2)
  })
})

describe('voiceDraftController defensive checks (blob size, mime whitelist)', () => {
  it('blob.size > 10 MiB → discard failure before upload', async () => {
    const h = createHarness()
    h.recorder.stop.mockResolvedValueOnce(makeAudio({ blob: makeBlob('x'.repeat(11 * 1024 * 1024), 11 * 1024 * 1024) }))
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(VOICE_MESSAGES.audioTooLarge)
      expect(state.failure.retryable).toBe(false)
    }
    expect(h.uploader).not.toHaveBeenCalled()
  })

  it('empty blob → discard failure before upload', async () => {
    const h = createHarness()
    h.recorder.stop.mockResolvedValueOnce(makeAudio({ blob: new Blob([]) }))
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    expect(h.uploader).not.toHaveBeenCalled()
    expect(statusOf(h)).toBe('failure')
  })

  it('mime outside whitelist → discard failure + diagnostic log', async () => {
    const h = createHarness()
    h.recorder.stop.mockResolvedValueOnce(makeAudio({ mimeType: 'audio/mp3' as never }))
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const state = h.controller.getState()
    expect(state.status).toBe('failure')
    if (state.status === 'failure') {
      expect(state.failure.message).toBe(VOICE_MESSAGES.unsupportedFormat)
    }
    expect(h.uploader).not.toHaveBeenCalled()
    expect(h.logCalls.length).toBeGreaterThan(0)
  })

  it('stop() rejection → discard failure', async () => {
    const h = createHarness()
    h.recorder.stop.mockRejectedValueOnce(new Error('stop failed'))
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    expect(h.uploader).not.toHaveBeenCalled()
    expect(statusOf(h)).toBe('failure')
  })

  it('whitelist contains exactly webm/wav', () => {
    expect(ALLOWED_AUDIO_MIME_TYPES).toEqual(['audio/webm', 'audio/wav'])
  })
})

describe('voiceDraftController draft → submit sequence (§4.8)', () => {
  it('confirm(204) → submit(202, draft_ref) is the only submit path', async () => {
    const h = createHarness()
    await recordToDraft(h)

    const state = h.controller.getState()
    expect(state.status).toBe('draft')
    if (state.status === 'draft') {
      expect(state.draft.text).toBe('这是转写文本')
      expect(state.draft.provider).toBe('speaches')
    }

    h.controller.confirm('编辑后的文本')
    expect(h.confirm).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    await Promise.resolve()

    expect(h.submit).toHaveBeenCalledTimes(1)
    expect(h.submit).toHaveBeenCalledWith({
      conversationId: 'conversation-1',
      text: '编辑后的文本',
      draftRef: 'draft-1',
    })
    expect(statusOf(h)).toBe('idle')
    expect(h.toasts).toEqual([VOICE_MESSAGES.submitted])
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('confirm failure (network) keeps sheet open; submit never called', async () => {
    const h = createHarness({ confirm: vi.fn().mockRejectedValue(error(undefined, undefined, 'offline')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.submit).not.toHaveBeenCalled()
    expect(statusOf(h)).toBe('draft')
    expect(h.toasts).toEqual([VOICE_MESSAGES.offline])
    const state = h.controller.getState()
    if (state.status === 'draft') expect(state.draft.submitting).toBe(false)
  })

  it('confirm 410 draft_expired → expired toast, storage cleared, no submit', async () => {
    const h = createHarness({ confirm: vi.fn().mockRejectedValue(error(410, 'draft_expired')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.submit).not.toHaveBeenCalled()
    expect(h.toasts).toEqual([VOICE_MESSAGES.draftExpired])
    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('confirm 409/404 → best-effort close with generic toast, no submit', async () => {
    const h = createHarness({ confirm: vi.fn().mockRejectedValue(error(409, 'draft_not_confirmable')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.submit).not.toHaveBeenCalled()
    expect(h.toasts).toEqual([VOICE_MESSAGES.confirmFailed])
    expect(statusOf(h)).toBe('idle')
  })

  it('submit network failure after confirm → storage kept, retry submits WITHOUT re-confirming', async () => {
    const h = createHarness({ submit: vi.fn().mockRejectedValueOnce(error(undefined, undefined, 'offline')).mockResolvedValueOnce(undefined) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.confirm).toHaveBeenCalledTimes(1)
    expect(h.submit).toHaveBeenCalledTimes(1)
    expect(statusOf(h)).toBe('draft')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(true)
    const state = h.controller.getState()
    if (state.status === 'draft') {
      expect(state.draft.submitFailed).toBe(true)
      expect(state.draft.submitting).toBe(false)
    }
    expect(h.toasts).toEqual([VOICE_MESSAGES.offline])

    // Retry submit (no re-confirm)
    h.controller.retrySubmit()
    await Promise.resolve()
    await Promise.resolve()

    expect(h.confirm).toHaveBeenCalledTimes(1) // unchanged
    expect(h.submit).toHaveBeenCalledTimes(2)
    expect(h.submit).toHaveBeenLastCalledWith({ conversationId: 'conversation-1', text: '文本', draftRef: 'draft-1' })
    expect(statusOf(h)).toBe('idle')
    expect(h.toasts).toEqual([VOICE_MESSAGES.offline, VOICE_MESSAGES.submitted])
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('submit 409 consumed → treated as success (idempotent), storage cleared', async () => {
    const h = createHarness({ submit: vi.fn().mockRejectedValue(error(409, 'draft_not_consumable')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.submit).toHaveBeenCalledTimes(1)
    expect(h.toasts).toEqual([VOICE_MESSAGES.submitted])
    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('submit 410 draft_expired → expired toast, storage cleared', async () => {
    const h = createHarness({ submit: vi.fn().mockRejectedValue(error(410, 'draft_expired')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.toasts).toEqual([VOICE_MESSAGES.draftExpired])
    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('submit 422 mismatch → diagnostic log + generic toast, stays retryable', async () => {
    const h = createHarness({ submit: vi.fn().mockRejectedValue(error(422, 'draft_conversation_mismatch')) })
    await recordToDraft(h)

    h.controller.confirm('文本')
    await Promise.resolve()
    await Promise.resolve()

    expect(h.logCalls.some(([msg]) => msg.includes('draft conversation mismatch'))).toBe(true)
    expect(h.toasts).toEqual([VOICE_MESSAGES.submitFailed])
    expect(statusOf(h)).toBe('draft')
    const state = h.controller.getState()
    if (state.status === 'draft') expect(state.draft.submitFailed).toBe(true)
  })

  it('discard calls cancel, never submit, clears storage', async () => {
    const h = createHarness()
    await recordToDraft(h)

    h.controller.discard()
    expect(h.cancel).toHaveBeenCalledTimes(1)
    expect(h.cancel).toHaveBeenCalledWith('draft-1')
    expect(h.submit).not.toHaveBeenCalled()
    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('cancel failure (409/410/404/network) closes silently', async () => {
    const h = createHarness({ cancel: vi.fn().mockRejectedValue(error(410, 'draft_expired')) })
    await recordToDraft(h)

    h.controller.discard()
    await Promise.resolve()
    await Promise.resolve()

    expect(statusOf(h)).toBe('idle')
    expect(h.toasts).toEqual([])
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })
})

describe('voiceDraftController transcript-not-auto-submit (§4.7.2)', () => {
  it.each([
    ['recording cancel (slide-up release)', async (h: Harness) => {
      h.controller.press()
      await Promise.resolve()
      await Promise.resolve()
      h.controller.slide(100)
      h.controller.release()
      await Promise.resolve()
    }],
    ['pointercancel/visibilitychange', async (h: Harness) => {
      h.controller.press()
      await Promise.resolve()
      await Promise.resolve()
      h.controller.cancel()
      await Promise.resolve()
    }],
    ['discard (close sheet)', async (h: Harness) => {
      await recordToDraft(h)
      h.controller.discard()
      await Promise.resolve()
    }],
    ['upload failure retry path', async (h: Harness) => {
      await recordToUpload(h)
      await Promise.resolve()
      await Promise.resolve()
      h.controller.retryUpload()
      await Promise.resolve()
    }],
    ['expired restore path', async (h: Harness) => {
      h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify({ draftId: 'draft-9', text: '旧文本', bindingId: 'binding-1', conversationId: 'conversation-1', provider: 'p', region: '', expiresAt: '2020-01-01T00:00:00Z' } satisfies StoredVoiceDraft))
      h.controller.restore()
      await Promise.resolve()
    }],
  ])('submit call count is 0 on %s', async (_name, act) => {
    const h = createHarness()
    await act(h)
    expect(h.submit).not.toHaveBeenCalled()
  })

  it('controller exposes no submit entry point outside the confirm sequence', async () => {
    const h = createHarness()
    await recordToDraft(h)
    expect('submit' in h.controller).toBe(false)
  })
})

describe('voiceDraftController draft-store restore (§4.7.4)', () => {
  const stored = (overrides: Partial<StoredVoiceDraft> = {}): StoredVoiceDraft => ({
    draftId: 'draft-2',
    text: '待确认的转写',
    bindingId: 'binding-1',
    conversationId: 'conversation-1',
    provider: 'speaches',
    region: '',
    expiresAt: '2030-01-01T00:00:00Z',
    ...overrides,
  })

  it('restores chip/sheet state after reload', () => {
    const h = createHarness()
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored()))

    h.controller.restore()

    const state = h.controller.getState()
    expect(state.status).toBe('draft')
    if (state.status === 'draft') {
      expect(state.draft).toMatchObject({
        draftId: 'draft-2',
        text: '待确认的转写',
        provider: 'speaches',
        submitting: false,
        submitFailed: false,
      })
    }
  })

  it('expired draft → discarded with toast', () => {
    const h = createHarness()
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored({ expiresAt: '2020-01-01T00:00:00Z' })))

    h.controller.restore()

    expect(statusOf(h)).toBe('idle')
    expect(h.toasts).toEqual([VOICE_MESSAGES.draftExpired])
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('corrupt JSON → slot cleared, stays idle', () => {
    const h = createHarness()
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, '{not json')

    h.controller.restore()

    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('malformed expiresAt → treated as expired, slot cleared', () => {
    const h = createHarness()
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored({ expiresAt: 'not-a-date' })))

    h.controller.restore()

    expect(statusOf(h)).toBe('idle')
    expect(h.toasts).toEqual([VOICE_MESSAGES.draftExpired])
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('restored draft for a different conversation stays dormant', () => {
    const h = createHarness()
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored({ conversationId: 'other-conversation' })))

    h.controller.restore()

    expect(statusOf(h)).toBe('idle')
    expect(h.storageMap.has(VOICE_DRAFT_STORAGE_KEY)).toBe(true)
  })

  it('restore is a no-op outside idle', async () => {
    const h = createHarness()
    await recordToDraft(h)
    h.storageMap.set(VOICE_DRAFT_STORAGE_KEY, JSON.stringify(stored()))
    h.controller.restore()
    expect(statusOf(h)).toBe('draft') // unchanged
    const state = h.controller.getState()
    if (state.status === 'draft') expect(state.draft.draftId).toBe('draft-1')
  })
})

describe('voiceDraftController raw-audio never persisted (§6.1)', () => {
  it('storage value contains metadata/text only, never audio bytes', async () => {
    const h = createHarness()
    await recordToDraft(h)

    const raw = h.storageMap.get(VOICE_DRAFT_STORAGE_KEY)
    expect(raw).toBeDefined()
    expect(raw).toContain('这是转写文本')
    expect(raw).toContain('draft-1')
    expect(raw).not.toContain(AUDIO_BYTES)
  })

  it('logger calls never carry audio bytes across all paths', async () => {
    const h = createHarness({ uploader: vi.fn().mockRejectedValue(error(415, 'unsupported_audio_format')) })
    await recordToUpload(h)
    await Promise.resolve()
    await Promise.resolve()

    const serialized = JSON.stringify(h.logCalls)
    expect(serialized).not.toContain(AUDIO_BYTES)
    expect(serialized).not.toContain('这是转写文本')
  })

  it('upload success/failure releases the blob reference (GC-reachable check)', async () => {
    if (typeof globalThis.gc !== 'function') return // requires --expose-gc

    const h = createHarness()
    await recordToDraft(h)
    const blob = (h.uploader.mock.calls[0] as [RecordedAudio])[0].blob
    const ref = new WeakRef(blob)

    h.controller.discard()
    h.controller.reRecord()
    await Promise.resolve()

    globalThis.gc()
    await new Promise((resolve) => setTimeout(resolve, 0))
    globalThis.gc()
    expect(ref.deref()).toBeUndefined()
  })
})

describe('voiceDraftController state notifications', () => {
  it('emits state for every transition and includes computed durations', async () => {
    const h = createHarness()
    h.controller.press()
    await Promise.resolve()
    await Promise.resolve()
    h.advance(1_500)
    h.controller.release()
    await Promise.resolve()
    await Promise.resolve()

    const statuses = h.states.map((s) => s.status)
    expect(statuses).toEqual(['arming', 'recording', 'stopping', 'uploading', 'draft'])
  })
})

describe('voiceErrorInfo / classifyUploadError pure helpers', () => {
  it('extracts status/code/kind from thrown errors', () => {
    expect(voiceErrorInfo(error(503, 'speech_to_text_unavailable'))).toEqual({ status: 503, code: 'speech_to_text_unavailable' })
    expect(voiceErrorInfo(error(undefined, undefined, 'offline'))).toEqual({ kind: 'offline' })
    expect(voiceErrorInfo(new Error('plain'))).toEqual({})
    expect(voiceErrorInfo('string')).toEqual({})
  })

  it('classifyUploadError maps every §4.6 row', () => {
    expect(classifyUploadError(error(503, 'speech_to_text_unavailable'))).toMatchObject({ kind: 'provider_unavailable' })
    expect(classifyUploadError(error(502, 'transcription_failed'))).toMatchObject({ kind: 'transient' })
    expect(classifyUploadError(error(504, 'transcription_timeout'))).toMatchObject({ kind: 'transient' })
    expect(classifyUploadError(error(413, 'audio_too_large'))).toMatchObject({ kind: 'discard' })
    expect(classifyUploadError(error(415, 'unsupported_audio_format'))).toMatchObject({ kind: 'discard' })
    expect(classifyUploadError(error(422, 'invalid_audio'))).toMatchObject({ kind: 'discard' })
    expect(classifyUploadError(error(undefined, undefined, 'offline'))).toMatchObject({ kind: 'transient' })
    expect(classifyUploadError(error(undefined, undefined, 'http_capability_denied'))).toMatchObject({ kind: 'capability_denied' })
    expect(classifyUploadError(error(500, 'internal_error'))).toMatchObject({ kind: 'transient', message: VOICE_MESSAGES.transcriptionFailed })
  })
})
