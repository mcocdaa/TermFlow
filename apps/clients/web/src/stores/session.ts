import { reactive } from 'vue'
import { createSession, deleteSession, getSessionStatus } from '../api/session'

export const sessionState = reactive({ authenticated: false, expiresAt: null as string | null })

export async function refreshSession(signal?: AbortSignal) {
  try {
    const session = await getSessionStatus(signal)
    sessionState.authenticated = session.authenticated
    sessionState.expiresAt = session.expires_at ?? null
    return session
  } catch {
    sessionState.authenticated = false
    sessionState.expiresAt = null
    return { authenticated: false }
  }
}

export async function loginWithToken(token: string, signal?: AbortSignal) {
  const session = await createSession(token, signal)
  sessionState.authenticated = session.authenticated
  sessionState.expiresAt = session.expires_at ?? null
  return session
}

export async function logoutSession(signal?: AbortSignal) {
  try { await deleteSession(signal) } finally {
    sessionState.authenticated = false
    sessionState.expiresAt = null
  }
}
