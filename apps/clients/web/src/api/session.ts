import type { SessionStatusDto } from './types'
import { apiRequest } from './http'

export const getSessionStatus = (signal?: AbortSignal) => apiRequest<SessionStatusDto>('/session', { signal })
export const createSession = (adminToken: string, signal?: AbortSignal) => apiRequest<SessionStatusDto>('/session', {
  method: 'POST',
  signal,
  body: { admin_token: adminToken },
})
export const deleteSession = (signal?: AbortSignal) => apiRequest<void>('/session', { method: 'DELETE', signal })
