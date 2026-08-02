import type { TermSummary, TopologyResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createTermsApi(request: ApiRequest) {
  return {
    topology: (id: string, signal?: AbortSignal) => request<TopologyResponse>(`/api/v1/instances/${encodeURIComponent(id)}/topology`, withSignal({}, signal)),
    rename: (id: string, name: string, signal?: AbortSignal) => request<TermSummary>(`/api/v1/terms/${encodeURIComponent(id)}`, withSignal({
      method: 'PATCH',
      body: { name },
    }, signal)),
    remove: (id: string, signal?: AbortSignal) => request<void>(`/api/v1/terms/${encodeURIComponent(id)}`, withSignal({
      method: 'DELETE',
    }, signal)),
  }
}
