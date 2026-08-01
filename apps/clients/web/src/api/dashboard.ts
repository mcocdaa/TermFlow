import type { DashboardDto } from './types'
import { apiRequest } from './http'

export const getDashboard = (signal?: AbortSignal) => apiRequest<DashboardDto>('/dashboard', { signal })
