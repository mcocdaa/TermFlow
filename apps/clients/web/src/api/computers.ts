import { apiRequest } from './http'
import type { ComputerDetailDto, ComputerListDto, EnrollmentCodeDto } from './types'

export const listComputers = (signal?: AbortSignal) => apiRequest<ComputerListDto>('/computers', { signal })
export const getComputer = (id: string, signal?: AbortSignal) => apiRequest<ComputerDetailDto>(`/computers/${encodeURIComponent(id)}`, { signal })
export const renameComputer = (id: string, displayName: string, signal?: AbortSignal) => apiRequest<ComputerDetailDto>(`/computers/${encodeURIComponent(id)}`, { method: 'PATCH', signal, body: { display_name: displayName } })
export const createEnrollmentCode = (displayName?: string, signal?: AbortSignal) => apiRequest<EnrollmentCodeDto>('/enrollment-tokens', {
  method: 'POST',
  signal,
  body: displayName === undefined ? undefined : { display_name: displayName },
})
