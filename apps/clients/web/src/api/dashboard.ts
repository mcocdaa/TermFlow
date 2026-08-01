import type { DashboardDto } from './types'
import { browserApiClient } from './http'

export const getDashboard = (signal?: AbortSignal): Promise<DashboardDto> => browserApiClient.dashboard.get(signal)
