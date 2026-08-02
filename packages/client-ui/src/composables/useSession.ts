import type { ApiClient } from '@termflow/client-core'
import { reactive, readonly } from 'vue'
import { useClientRuntime } from '../runtime'

export const sessionState = reactive({ authenticated: false, expiresAt: null as string | null })

export function clearSessionState() {
  sessionState.authenticated = false
  sessionState.expiresAt = null
}

export function createSessionActions(api: ApiClient) {
  return {
    async refreshSession(signal?: AbortSignal) {
      try {
        const session = signal === undefined ? await api.sessions.status() : await api.sessions.status(signal)
        sessionState.authenticated = session.authenticated
        sessionState.expiresAt = session.expires_at ?? null
        return session
      } catch {
        clearSessionState()
        return { authenticated: false }
      }
    },
    async loginWithToken(token: string, signal?: AbortSignal) {
      const session = signal === undefined ? await api.sessions.login(token) : await api.sessions.login(token, signal)
      sessionState.authenticated = session.authenticated
      sessionState.expiresAt = session.expires_at ?? null
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

export function useSession() {
  const runtime = useClientRuntime()
  return { sessionState: readonly(sessionState), ...createSessionActions(runtime.api) }
}
