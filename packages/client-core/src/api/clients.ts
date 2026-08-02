import type { NativeClientDeleteResponse, NativeClientListResponse, NativeClientResponse, OAuthScope } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'
import type { SecurityReauthentication } from './security'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

function reauthBody(reauth: SecurityReauthentication): Record<string, string> {
  return { admin_token: reauth.adminToken, ...(reauth.totpCode === undefined ? {} : { totp_code: reauth.totpCode }) }
}

export function createClientsApi(request: ApiRequest) {
  return {
    list: (signal?: AbortSignal) => request<NativeClientListResponse>('/api/v1/admin/clients', withSignal({}, signal)),
    update: (clientId: string, displayName: string, scopes: OAuthScope[], reauth: SecurityReauthentication, signal?: AbortSignal) => request<NativeClientResponse>(`/api/v1/admin/clients/${encodeURIComponent(clientId)}`, withSignal({
      method: 'PATCH', body: { display_name: displayName, scopes, ...reauthBody(reauth) },
    }, signal)),
    remove: (clientId: string, reauth: SecurityReauthentication, signal?: AbortSignal) => request<NativeClientDeleteResponse>(`/api/v1/admin/clients/${encodeURIComponent(clientId)}`, withSignal({
      method: 'DELETE', body: reauthBody(reauth),
    }, signal)),
  }
}
