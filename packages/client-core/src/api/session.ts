import type { BrowserSessionChallengeResponse, BrowserSessionDeleteResponse, BrowserSessionResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createSessionApi(request: ApiRequest) {
  return {
    status: (signal?: AbortSignal) => request<BrowserSessionResponse>('/api/v1/admin/session', withSignal({}, signal)),
    login: (adminToken: string, signal?: AbortSignal) => request<BrowserSessionResponse | BrowserSessionChallengeResponse>('/api/v1/admin/sessions', withSignal({
      method: 'POST',
      body: { admin_token: adminToken },
    }, signal)),
    completeTotp: (challengeId: string, code: string, signal?: AbortSignal) => request<BrowserSessionResponse>(`/api/v1/admin/sessions/${encodeURIComponent(challengeId)}/totp`, withSignal({
      method: 'POST',
      body: { code },
    }, signal)),
    logout: (signal?: AbortSignal) => request<BrowserSessionDeleteResponse | undefined>('/api/v1/admin/session', withSignal({ method: 'DELETE' }, signal)),
  }
}
