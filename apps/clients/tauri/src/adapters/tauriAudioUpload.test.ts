import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AudioUploadParams, RecordedAudio } from '@termflow/client-core'

const { invoke, logNativeEvent } = vi.hoisted(() => ({
  invoke: vi.fn(),
  logNativeEvent: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))
vi.mock('../diagnostics', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../diagnostics')>()
  return { ...actual, logNativeEvent }
})

import { serverConfig } from '../serverConfig'
import { blobToBase64, createTauriAudioUpload } from './tauriAudioUpload'

function nativeResponse(overrides: Partial<{ status: number; headers: Record<string, string>; body: unknown }> = {}) {
  return {
    status: overrides.status ?? 200,
    headers: overrides.headers ?? {},
    body: overrides.body ?? undefined,
  }
}

const draftResponse = {
  draft_id: 'draft-1',
  state: 'pending',
  transcript: '你好，转写文本',
  provider: 'speaches',
  region: 'cn-1',
  language: 'zh-CN',
  duration_seconds: 2.5,
  expires_at: '2026-08-15T12:00:00Z',
}

function webmAudio(): RecordedAudio {
  return {
    blob: new Blob([new Uint8Array([0, 1, 2, 255])], { type: 'audio/webm' }),
    mimeType: 'audio/webm',
    durationSeconds: 2.5,
  }
}

function uploadParams(signal?: AbortSignal): AudioUploadParams {
  return { bindingId: 'binding-1', conversationId: 'conv-1', signal: signal ?? new AbortController().signal }
}

describe('createTauriAudioUpload (§4.5/§4.6)', () => {
  beforeEach(() => {
    invoke.mockReset()
    logNativeEvent.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('uploads base64 through native_upload_audio and returns the parsed 201 draft', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 201, body: draftResponse }))

    const result = await createTauriAudioUpload()(webmAudio(), uploadParams())

    expect(result).toEqual(draftResponse)
    expect(invoke).toHaveBeenCalledTimes(1)
    expect(invoke).toHaveBeenCalledWith('native_upload_audio', {
      issuer: 'https://b.example',
      audioBase64: 'AAEC/w==',
      mime: 'audio/webm',
      bindingId: 'binding-1',
      conversationId: 'conv-1',
    })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'audio_upload_response' }))
  })

  it('retries exactly once with the DPoP nonce on a 401 challenge', async () => {
    invoke
      .mockResolvedValueOnce(nativeResponse({ status: 401, headers: { 'dpop-nonce': 'nonce-1' } }))
      .mockResolvedValueOnce(nativeResponse({ status: 201, body: draftResponse }))

    const result = await createTauriAudioUpload()(webmAudio(), uploadParams())

    expect(result.draft_id).toBe('draft-1')
    expect(invoke).toHaveBeenCalledTimes(2)
    expect(invoke).toHaveBeenNthCalledWith(2, 'native_upload_audio', expect.objectContaining({ nonce: 'nonce-1' }))
  })

  it('surfaces B error envelopes with their HTTP status and public code', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 503, body: { error: { code: 'speech_to_text_unavailable' } } }))

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({
      status: 503,
      code: 'speech_to_text_unavailable',
    })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({ errorCode: 'http_503' }))
  })

  it('surfaces envelopes without an error code as bare status', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 413, body: 'plain text' }))

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({ status: 413 })
  })

  it('rejects a malformed 201 body as a retryable protocol violation', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 201, body: { draft_id: 42 } }))

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({
      status: 502,
      code: 'transcription_failed',
    })
  })

  it('normalizes absent optional 201 fields to null', async () => {
    invoke.mockResolvedValue(nativeResponse({ status: 201, body: { ...draftResponse, language: undefined, duration_seconds: undefined } }))

    const result = await createTauriAudioUpload()(webmAudio(), uploadParams())

    expect(result.language).toBeNull()
    expect(result.duration_seconds).toBeNull()
  })

  it('rejects a mime outside the webm/wav whitelist before any invoke', async () => {
    const audio: RecordedAudio = {
      blob: new Blob(['x']),
      mimeType: 'audio/mp4' as unknown as RecordedAudio['mimeType'],
      durationSeconds: 1,
    }

    await expect(createTauriAudioUpload()(audio, uploadParams())).rejects.toEqual({
      status: 415,
      code: 'unsupported_audio_format',
    })
    expect(invoke).not.toHaveBeenCalled()
  })

  it.each(['url_not_allowed', 'method_not_allowed'])('maps the native %s denial to http_capability_denied', async (code) => {
    invoke.mockRejectedValue(code)

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({ kind: 'http_capability_denied' })
    expect(logNativeEvent).toHaveBeenCalledWith(
      expect.objectContaining({ event: 'audio_upload_failed', errorCode: 'http_capability_denied', level: 'error' }),
    )
  })

  it('maps the Rust audio_too_large defense onto the B side 413 semantics', async () => {
    invoke.mockRejectedValue('audio_too_large')

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({ status: 413, code: 'audio_too_large' })
  })

  it('maps the Rust audio_mime_not_allowed defense onto the B side 415 semantics', async () => {
    invoke.mockRejectedValue('audio_mime_not_allowed')

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({
      status: 415,
      code: 'unsupported_audio_format',
    })
  })

  it('classifies ordinary native failures as offline', async () => {
    invoke.mockRejectedValue('request_failed')

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams())).rejects.toEqual({ kind: 'offline' })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'audio_upload_failed', errorCode: 'offline' }))
  })

  it('rejects with kind aborted when the signal fires mid-flight', async () => {
    invoke.mockImplementation(() => new Promise(() => {}))
    const controller = new AbortController()
    const pending = createTauriAudioUpload()(webmAudio(), uploadParams(controller.signal))
    controller.abort()

    await expect(pending).rejects.toEqual({ kind: 'aborted' })
  })

  it('rejects with kind aborted when the signal was already aborted', async () => {
    const controller = new AbortController()
    controller.abort()

    await expect(createTauriAudioUpload()(webmAudio(), uploadParams(controller.signal))).rejects.toEqual({ kind: 'aborted' })
    expect(invoke).not.toHaveBeenCalled()
  })
})

describe('blobToBase64', () => {
  it('encodes blob bytes as raw base64 without a data-URL prefix', async () => {
    await expect(blobToBase64(new Blob([new Uint8Array([0, 1, 2, 255])], { type: 'audio/wav' }))).resolves.toBe('AAEC/w==')
  })

  it('encodes empty blobs as the empty string', async () => {
    await expect(blobToBase64(new Blob([]))).resolves.toBe('')
  })
})
