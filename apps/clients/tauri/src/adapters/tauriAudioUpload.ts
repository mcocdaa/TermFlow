/**
 * Tauri audio-upload adapter (M7b spec §4.5/§4.6): encodes the in-memory
 * recording blob as base64 and hands it to the Rust `native_upload_audio`
 * command, which owns the multipart request + DPoP. The JS side only parses
 * the resulting envelope.
 *
 * Command contract (§4.5, implemented on the Rust side in a follow-up task):
 *   native_upload_audio { issuer, audioBase64, mime, bindingId,
 *                         conversationId, nonce? }
 *     → NativeHttpResponse { status, headers, body }
 * Rust rejects are `String` safe-errors: `url_not_allowed`/
 * `method_not_allowed` (capability denial), `audio_too_large`,
 * `audio_mime_not_allowed`, `offline`. The 401 + `dpop-nonce` retry loop
 * mirrors `tauriHttpTransport`.
 *
 * Thrown errors carry the `{ status?, code?, kind? }` shape the client-core
 * `classifyUploadError` maps to copy/actions per §4.6:
 *   B error envelopes ({ error: { code } }) keep their HTTP status/code;
 *   transport failures become kind 'offline'; aborts become kind 'aborted';
 *   capability denials become kind 'http_capability_denied'.
 */

import { invoke } from '@tauri-apps/api/core'
import type {
  AudioUploader,
  AudioUploadParams,
  RecordedAudio,
  TranscriptionUploadResponse,
} from '@termflow/client-core'
import { logNativeEvent, sanitizeNativeDetail } from '../diagnostics'
import { serverConfig } from '../serverConfig'

/** Rust command name — keep in sync with src-tauri (registered in the follow-up subtask). */
export const UPLOAD_COMMAND = 'native_upload_audio'

/** Client-side mime whitelist (M7b spec §4.3.1/§4.6): the B side only accepts webm/wav. */
export const ALLOWED_UPLOAD_MIME_TYPES: readonly string[] = ['audio/webm', 'audio/wav']

/** Error shape consumed by client-core `classifyUploadError` (§4.6). */
export interface UploadErrorInfo {
  status?: number
  code?: string
  kind?: 'offline' | 'aborted' | 'http_capability_denied'
}

interface NativeHttpResponse {
  status: number
  headers: Record<string, string>
  body: unknown
}

/**
 * Encode a blob as raw base64 (no data-URL prefix) through the platform
 * FileReader. 10 MiB worst case yields ~13.4 MB of base64 text, which the
 * IPC JSON channel handles (spec §7 risk 3 measures real latency at M7 exit).
 */
export function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result !== 'string') {
        reject(new Error('base64_encode_failed'))
        return
      }
      const comma = result.indexOf(',')
      resolve(comma === -1 ? result : result.slice(comma + 1))
    }
    reader.onerror = () => reject(new Error('base64_encode_failed'))
    reader.readAsDataURL(blob)
  })
}

function errorText(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : typeof error === 'string' ? error : ''
}

function uploadError(info: UploadErrorInfo): UploadErrorInfo {
  return info
}

/** Mirror the `tauriHttpTransport` nonce challenge: exactly one retry. */
function raceWithAbort<T>(signal: AbortSignal, run: () => Promise<T>): Promise<T> {
  if (signal.aborted) return Promise.reject(uploadError({ kind: 'aborted' }))
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(uploadError({ kind: 'aborted' }))
    signal.addEventListener('abort', onAbort, { once: true })
    run().then(
      (value) => {
        signal.removeEventListener('abort', onAbort)
        resolve(value)
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort)
        reject(error)
      },
    )
  })
}

/** Classify invoke rejections: capability denial / size / mime / offline. */
function mapUploadFailure(error: unknown): UploadErrorInfo {
  const candidate = error as UploadErrorInfo | null
  if (
    candidate !== null &&
    typeof candidate === 'object' &&
    (candidate.kind === 'offline' || candidate.kind === 'aborted' || candidate.kind === 'http_capability_denied')
  ) {
    return candidate
  }
  const text = errorText(error)
  if (text.includes('url_not_allowed') || text.includes('method_not_allowed')) {
    void logNativeEvent({
      event: 'audio_upload_failed',
      issuer: serverConfig.current,
      level: 'error',
      errorCode: 'http_capability_denied',
      errorDetail: sanitizeNativeDetail(error),
    })
    return { kind: 'http_capability_denied' }
  }
  // Rust-side defenses (§4.5) surface as string safe-errors before any
  // network activity — map them onto the B side 413/415 semantics so the
  // controller discards the blob and asks for a re-record.
  if (text.includes('audio_too_large')) return { status: 413, code: 'audio_too_large' }
  if (text.includes('audio_mime_not_allowed')) return { status: 415, code: 'unsupported_audio_format' }
  void logNativeEvent({
    event: 'audio_upload_failed',
    issuer: serverConfig.current,
    level: 'error',
    errorCode: 'offline',
    errorDetail: sanitizeNativeDetail(error),
  })
  return { kind: 'offline' }
}

/** B error envelopes keep their HTTP status and public `error.code`. */
function parseErrorEnvelope(status: number, body: unknown): UploadErrorInfo {
  let code: string | undefined
  if (typeof body === 'object' && body !== null) {
    const envelope = body as { error?: { code?: unknown } }
    if (typeof envelope.error === 'object' && envelope.error !== null && typeof envelope.error.code === 'string') {
      code = envelope.error.code
    }
  }
  return { status, ...(code === undefined ? {} : { code }) }
}

const DRAFT_RESPONSE_PROTOCOL_VIOLATION: UploadErrorInfo = { status: 502, code: 'transcription_failed' }

/**
 * Validate and narrow the 201 body to the client-core `TranscriptionUploadResponse`
 * shape (mirrors generated.ts `TranscriptionDraftResponse`). A malformed body
 * is a protocol violation — surface it as a retryable 502 with the blob kept.
 */
function parseDraftResponse(body: unknown): TranscriptionUploadResponse {
  if (typeof body !== 'object' || body === null) throw DRAFT_RESPONSE_PROTOCOL_VIOLATION
  const candidate = body as Record<string, unknown>
  if (
    typeof candidate.draft_id !== 'string' ||
    typeof candidate.state !== 'string' ||
    typeof candidate.transcript !== 'string' ||
    typeof candidate.provider !== 'string' ||
    typeof candidate.region !== 'string' ||
    typeof candidate.expires_at !== 'string'
  ) {
    throw DRAFT_RESPONSE_PROTOCOL_VIOLATION
  }
  return {
    draft_id: candidate.draft_id,
    state: candidate.state,
    transcript: candidate.transcript,
    provider: candidate.provider,
    region: candidate.region,
    language: typeof candidate.language === 'string' ? candidate.language : null,
    duration_seconds: typeof candidate.duration_seconds === 'number' ? candidate.duration_seconds : null,
    expires_at: candidate.expires_at,
  }
}

export function createTauriAudioUpload(): AudioUploader {
  return async (audio: RecordedAudio, params: AudioUploadParams): Promise<TranscriptionUploadResponse> => {
    if (!ALLOWED_UPLOAD_MIME_TYPES.includes(audio.mimeType)) {
      throw uploadError({ status: 415, code: 'unsupported_audio_format' })
    }
    const audioBase64 = await blobToBase64(audio.blob)
    const send = (nonce?: string) => invoke<NativeHttpResponse>(UPLOAD_COMMAND, {
      issuer: serverConfig.current,
      audioBase64,
      mime: audio.mimeType,
      bindingId: params.bindingId,
      conversationId: params.conversationId,
      ...(nonce === undefined ? {} : { nonce }),
    })

    let response: NativeHttpResponse
    try {
      response = await raceWithAbort(params.signal, () => send())
      if (response.status === 401 && response.headers['dpop-nonce'] !== undefined) {
        response = await raceWithAbort(params.signal, () => send(response.headers['dpop-nonce']))
      }
    } catch (error) {
      throw mapUploadFailure(error)
    }

    void logNativeEvent({
      event: 'audio_upload_response',
      issuer: serverConfig.current,
      requestId: response.headers['x-request-id'] ?? undefined,
      errorCode: response.status >= 400 ? `http_${response.status}` : undefined,
    })

    if (response.status === 201) return parseDraftResponse(response.body)
    throw parseErrorEnvelope(response.status, response.body)
  }
}
