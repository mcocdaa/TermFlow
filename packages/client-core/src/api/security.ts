import type { CliTokenResponse, OAuthScope, TotpSetupResponse, TotpStatusResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

export interface SecurityReauthentication {
  adminToken: string
  totpCode?: string
}

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createSecurityApi(request: ApiRequest) {
  return {
    totpStatus: (signal?: AbortSignal) => request<TotpStatusResponse>('/api/v1/admin/totp', withSignal({}, signal)),
    createTotpSetup: (reauth: SecurityReauthentication, signal?: AbortSignal) => request<TotpSetupResponse>('/api/v1/admin/totp/setups', withSignal({
      method: 'POST',
      body: { admin_token: reauth.adminToken, ...(reauth.totpCode === undefined ? {} : { totp_code: reauth.totpCode }) },
    }, signal)),
    confirmTotpSetup: (setupId: string, code: string, signal?: AbortSignal) => request<TotpStatusResponse>(`/api/v1/admin/totp/setups/${encodeURIComponent(setupId)}/confirm`, withSignal({
      method: 'POST', body: { code },
    }, signal)),
    disableTotp: (reauth: Required<SecurityReauthentication>, signal?: AbortSignal) => request<void>('/api/v1/admin/totp', withSignal({
      method: 'DELETE', body: { admin_token: reauth.adminToken, code: reauth.totpCode },
    }, signal)),
    createCliToken: (reauth: SecurityReauthentication, scopes: OAuthScope[], signal?: AbortSignal) => request<CliTokenResponse>('/api/v1/admin/cli-tokens', withSignal({
      method: 'POST',
      body: { admin_token: reauth.adminToken, ...(reauth.totpCode === undefined ? {} : { totp_code: reauth.totpCode }), scopes },
    }, signal)),
  }
}
