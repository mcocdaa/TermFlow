import { apiRequest } from './http'
import type { TermDetailDto } from './types'

export const getTerm = (id: string, signal?: AbortSignal) => apiRequest<TermDetailDto>(`/terms/${encodeURIComponent(id)}`, { signal })
export const renameTerm = (id: string, name: string, signal?: AbortSignal) => apiRequest<TermDetailDto>(`/terms/${encodeURIComponent(id)}`, { method: 'PATCH', signal, body: { name } })
