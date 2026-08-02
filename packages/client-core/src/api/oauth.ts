import type { OAuthAuthorizationDecisionResponse, OAuthAuthorizationPreviewResponse, OAuthMetadataResponse } from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

export interface AuthorizationDecisionInput {
  transactionId: string
  decision: 'allow' | 'deny'
  adminToken: string
  totpCode?: string
}

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createOAuthApi(request: ApiRequest) {
  return {
    metadata: (signal?: AbortSignal) => request<OAuthMetadataResponse>('/.well-known/oauth-authorization-server', withSignal({}, signal)),
    authorizationPreview: (transactionId: string, signal?: AbortSignal) => request<OAuthAuthorizationPreviewResponse>(`/api/v1/oauth/authorize?transaction_id=${encodeURIComponent(transactionId)}`, withSignal({}, signal)),
    decideAuthorization: (input: AuthorizationDecisionInput, signal?: AbortSignal) => request<OAuthAuthorizationDecisionResponse>('/api/v1/oauth/authorize', withSignal({
      method: 'POST',
      body: {
        transaction_id: input.transactionId,
        decision: input.decision,
        admin_token: input.adminToken,
        ...(input.totpCode === undefined ? {} : { totp_code: input.totpCode }),
      },
    }, signal)),
  }
}
