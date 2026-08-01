import { apiRequest } from './http'
import type { TermSummaryDto, TopologyResponseDto } from './types'

export const getTermTopology = (id: string, signal?: AbortSignal) => apiRequest<TopologyResponseDto>(`/instances/${encodeURIComponent(id)}/topology`, { signal })
export const renameTerm = (id: string, name: string, signal?: AbortSignal) => apiRequest<TermSummaryDto>(`/terms/${encodeURIComponent(id)}`, { method: 'PATCH', signal, body: { name } })
