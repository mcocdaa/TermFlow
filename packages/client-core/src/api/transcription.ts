/**
 * Agent transcription draft lifecycle endpoints (M7b spec §3.1/§4.7/§4.8).
 *
 * JSON paths over the existing `HttpTransport`/`ApiRequest` pipeline. The
 * multipart upload itself does NOT go through here — it is a Rust-held
 * command injected into the voice controller as the uploader (§4.5).
 *
 * Note: `GET /drafts/{id}` returns metadata only — the transcript text is
 * returned exactly once, in the upload response, and is never re-fetchable.
 */

import type { ApiRequest, ApiRequestOptions } from '../http/types'

/**
 * Owner-only draft metadata (handwritten in client-core; the B side
 * deliberately omits the transcript text — M7b spec §3.1).
 */
export interface TranscriptionDraftDetail {
  draft_id: string
  binding_id: string
  target_conversation_id: string
  state: string
  provider: string
  region: string
  transcript_hash: string
  expires_at: string
  created_at: string
}

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

function draftPath(draftId: string, suffix: string): `/${string}` {
  return `/api/v1/agent/transcription/drafts/${encodeURIComponent(draftId)}${suffix}` as const
}

export function createTranscriptionApi(request: ApiRequest) {
  return {
    /** `POST /api/v1/agent/transcription/drafts/{id}/confirm` — 204 on success. */
    confirmDraft: (draftId: string, signal?: AbortSignal) =>
      request<void>(draftPath(draftId, '/confirm'), withSignal({ method: 'POST' }, signal)),

    /** `POST /api/v1/agent/transcription/drafts/{id}/cancel` — 204 on success. */
    cancelDraft: (draftId: string, signal?: AbortSignal) =>
      request<void>(draftPath(draftId, '/cancel'), withSignal({ method: 'POST' }, signal)),

    /** `GET /api/v1/agent/transcription/drafts/{id}` — metadata only, no transcript text. */
    getDraft: (draftId: string, signal?: AbortSignal) =>
      request<TranscriptionDraftDetail>(draftPath(draftId, ''), withSignal({}, signal)),
  }
}

export type TranscriptionApi = ReturnType<typeof createTranscriptionApi>
