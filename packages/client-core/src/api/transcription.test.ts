import { describe, expect, it, vi } from 'vitest'
import { createTranscriptionApi } from './transcription'

const DRAFT = '33333333-3333-4333-8333-333333333333'

describe('transcription API', () => {
  it('maps confirm/cancel/get to draft lifecycle paths and methods', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const api = createTranscriptionApi(request)

    await api.confirmDraft(DRAFT)
    await api.cancelDraft(DRAFT)
    await api.getDraft(DRAFT)

    expect(request.mock.calls).toEqual([
      [`/api/v1/agent/transcription/drafts/${DRAFT}/confirm`, { method: 'POST' }],
      [`/api/v1/agent/transcription/drafts/${DRAFT}/cancel`, { method: 'POST' }],
      [`/api/v1/agent/transcription/drafts/${DRAFT}`, {}],
    ])
  })

  it('URL-encodes the draft id segment', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const api = createTranscriptionApi(request)

    await api.getDraft(`${DRAFT} /x`)

    expect(request.mock.calls[0]?.[0]).toBe(
      `/api/v1/agent/transcription/drafts/${encodeURIComponent(`${DRAFT} /x`)}`,
    )
  })

  it('passes the AbortSignal through when provided and omits it otherwise', async () => {
    const request = vi.fn().mockResolvedValue(undefined)
    const api = createTranscriptionApi(request)

    const signal = new AbortController().signal
    await api.confirmDraft(DRAFT, signal)
    await api.cancelDraft(DRAFT)

    expect(request.mock.calls[0]).toEqual([
      `/api/v1/agent/transcription/drafts/${DRAFT}/confirm`,
      { method: 'POST', signal },
    ])
    expect(request.mock.calls[1]).toEqual([
      `/api/v1/agent/transcription/drafts/${DRAFT}/cancel`,
      { method: 'POST' },
    ])
  })

  it('returns the response body through the transport', async () => {
    const body = {
      draft_id: DRAFT,
      binding_id: 'b',
      target_conversation_id: 'c',
      state: 'transcribed',
      provider: 'speaches',
      region: '',
      transcript_hash: 'h',
      expires_at: '2026-08-13T13:00:00Z',
      created_at: '2026-08-13T12:00:00Z',
    }
    const request = vi.fn().mockResolvedValue(body)
    const api = createTranscriptionApi(request)

    await expect(api.getDraft(DRAFT)).resolves.toEqual(body)
  })

  it('propagates transport errors unchanged (envelope parsing stays in the transport)', async () => {
    const failure = Object.assign(new Error('x'), { status: 404, code: 'draft_not_found' })
    const request = vi.fn().mockRejectedValue(failure)
    const api = createTranscriptionApi(request)

    await expect(api.getDraft(DRAFT)).rejects.toBe(failure)
  })
})
