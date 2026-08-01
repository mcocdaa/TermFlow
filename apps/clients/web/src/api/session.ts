import type { SessionStatusDto } from './types'
import { apiRequest } from './http'

export const getSessionStatus = (signal?: AbortSignal) => apiRequest<SessionStatusDto>('/admin/session', { signal })
export const createSession = (adminToken: string, signal?: AbortSignal) => apiRequest<SessionStatusDto>('/admin/sessions', {
  method: 'POST',
  signal,
  body: { admin_token: adminToken },
})
export const deleteSession = (signal?: AbortSignal) => apiRequest<void>('/admin/session', { method: 'DELETE', signal })
