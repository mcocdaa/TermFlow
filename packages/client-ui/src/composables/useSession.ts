import type { ApiClient } from '@termflow/client-core'
import { inject, reactive, readonly } from 'vue'
import { sessionActionsKey } from '../runtimeKey'

export function createSessionActions(api: ApiClient) {
  const mutableState = reactive({ authenticated: false, expiresAt: null as string | null })
  function clearSessionState() {
    mutableState.authenticated = false
    mutableState.expiresAt = null
  }
  return {
    sessionState: readonly(mutableState),
    clearSessionState,
    async refreshSession(signal?: AbortSignal) {
      try {
        const session = signal === undefined ? await api.sessions.status() : await api.sessions.status(signal)
        mutableState.authenticated = session.authenticated
        mutableState.expiresAt = session.expires_at ?? null
        return session
      } catch {
        clearSessionState()
        return { authenticated: false }
      }
    },
    async loginWithToken(token: string, signal?: AbortSignal) {
      const session = signal === undefined ? await api.sessions.login(token) : await api.sessions.login(token, signal)
      mutableState.authenticated = session.authenticated
      mutableState.expiresAt = session.expires_at ?? null
      return session
    },
    async logoutSession(signal?: AbortSignal) {
      try {
        if (signal === undefined) await api.sessions.logout()
        else await api.sessions.logout(signal)
      } finally {
        clearSessionState()
      }
    },
  }
}

export type SessionActions = ReturnType<typeof createSessionActions>

export function useSession(): SessionActions {
  const session = inject(sessionActionsKey, undefined)
  if (session === undefined) throw new Error('TermFlow client session is not installed.')
  return session
}
