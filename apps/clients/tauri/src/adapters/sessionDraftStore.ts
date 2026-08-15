/**
 * Session-storage adapter for the pending transcript draft slot
 * (M7b spec §4.7.4). Lives in `adapters/` so the security-contract scan of
 * `runtime.ts` stays clean; mirrors the web `browserThemePreferences.ts`
 * precedent (composition-root platform adapters may touch platform storage).
 *
 * Only the transcript text + metadata is persisted here — never raw audio
 * (spec §4.1). The client-core controller owns the single-slot key
 * (`VOICE_DRAFT_STORAGE_KEY`) and clears it after confirm/cancel/expiry.
 */

import type { VoiceStorage } from '@termflow/client-core'

type SessionStorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export function createSessionDraftStore(storage: SessionStorageLike = globalThis.sessionStorage): VoiceStorage {
  return {
    getItem: (key) => storage.getItem(key),
    setItem: (key, value) => {
      storage.setItem(key, value)
    },
    removeItem: (key) => {
      storage.removeItem(key)
    },
  }
}
