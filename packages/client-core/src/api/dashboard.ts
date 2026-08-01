import type { DashboardResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(signal: AbortSignal | undefined): ApiRequestOptions {
  return signal === undefined ? {} : { signal }
}

export function createDashboardApi(request: ApiRequest) {
  return {
    get: (signal?: AbortSignal) => request<DashboardResponse>('/api/v1/dashboard', withSignal(signal)),
  }
}
