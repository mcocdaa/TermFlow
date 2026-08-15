import { beforeEach, describe, expect, it, vi } from 'vitest'
import { VOICE_DRAFT_STORAGE_KEY } from '@termflow/client-core'
import { createSessionDraftStore } from './sessionDraftStore'

describe('createSessionDraftStore (§4.7.4)', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('round-trips the pending draft through real session storage', () => {
    const store = createSessionDraftStore()
    const payload = JSON.stringify({ draftId: 'draft-1', text: '待确认的转写', bindingId: 'b1', conversationId: 'c1', provider: 'speaches', region: '', expiresAt: '2026-08-15T12:00:00Z' })

    expect(store.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    store.setItem(VOICE_DRAFT_STORAGE_KEY, payload)
    expect(store.getItem(VOICE_DRAFT_STORAGE_KEY)).toBe(payload)
    store.removeItem(VOICE_DRAFT_STORAGE_KEY)
    expect(store.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
  })

  it('delegates to an injected storage when provided', () => {
    const slots = new Map<string, string>()
    const backing = {
      getItem: (key: string) => slots.get(key) ?? null,
      setItem: (key: string, value: string) => {
        slots.set(key, value)
      },
      removeItem: (key: string) => {
        slots.delete(key)
      },
    }
    const store = createSessionDraftStore(backing)

    store.setItem('k', 'v')
    expect(store.getItem('k')).toBe('v')
    store.removeItem('k')
    expect(store.getItem('k')).toBeNull()
  })

  it('never exposes storage surface beyond the VoiceStorage port', () => {
    const setItem = vi.fn()
    const store = createSessionDraftStore({
      getItem: () => null,
      setItem,
      removeItem: () => undefined,
    })

    store.setItem('k', 'v')
    expect(setItem).toHaveBeenCalledWith('k', 'v')
    expect(Object.keys(store).sort()).toEqual(['getItem', 'removeItem', 'setItem'])
  })
})
