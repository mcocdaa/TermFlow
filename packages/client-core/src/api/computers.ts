import type { ComputerListResponse, ComputerSummary, EnrollmentCreateResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createComputersApi(request: ApiRequest) {
  return {
    list: (signal?: AbortSignal) => request<ComputerListResponse>('/api/v1/computers', withSignal({}, signal)),
    get: (id: string, signal?: AbortSignal) => request<ComputerSummary>(`/api/v1/computers/${encodeURIComponent(id)}`, withSignal({}, signal)),
    remove: (id: string, signal?: AbortSignal) => request<void>(`/api/v1/computers/${encodeURIComponent(id)}`, withSignal({
      method: 'DELETE',
    }, signal)),
    rename: (id: string, displayName: string, signal?: AbortSignal) => request<ComputerSummary>(`/api/v1/computers/${encodeURIComponent(id)}`, withSignal({
      method: 'PATCH',
      body: { display_name: displayName },
    }, signal)),
    createEnrollment: (displayName?: string, signal?: AbortSignal) => request<EnrollmentCreateResponse>('/api/v1/enrollment-tokens', withSignal({
      method: 'POST',
      body: displayName === undefined ? undefined : { display_name: displayName },
    }, signal)),
  }
}
