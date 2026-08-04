import type {
  OAuthAuthorizationDecisionResponse,
  OAuthAuthorizationPreviewResponse,
  OAuthDeviceCodeResponse,
  OAuthMetadataResponse,
  OAuthPublicJwk,
  OAuthTokenResponse,
  OAuthScope,
} from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

export interface AuthorizationDecisionInput {
  transactionId: string
  decision: 'allow' | 'deny'
  adminToken: string
  totpCode?: string
}

export interface DeviceAuthorizationInput {
  clientName: string
  platform: string
  clientVersion: string | null
  codeChallenge: string
  codeChallengeMethod?: 'S256'
  dpopJkt: string
  publicJwk: OAuthPublicJwk
  scopes: OAuthScope[]
}

export interface DeviceAuthorizationPollInput {
  deviceCode: string
  codeVerifier: string
  publicJwk: OAuthPublicJwk
}

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

export function createOAuthApi(request: ApiRequest) {
  return {
    metadata: (signal?: AbortSignal) => request<OAuthMetadataResponse>('/.well-known/oauth-authorization-server', withSignal({}, signal)),
    createDeviceAuthorization: (input: DeviceAuthorizationInput, signal?: AbortSignal) => request<OAuthDeviceCodeResponse>('/api/v1/oauth/device/code', withSignal({
      method: 'POST',
      body: {
        client_name: input.clientName,
        platform: input.platform,
        client_version: input.clientVersion,
        code_challenge: input.codeChallenge,
        code_challenge_method: input.codeChallengeMethod ?? 'S256',
        dpop_jkt: input.dpopJkt,
        public_jwk: input.publicJwk,
        scopes: input.scopes,
      },
    }, signal)),
    pollDeviceAuthorization: (input: DeviceAuthorizationPollInput, signal?: AbortSignal) => request<OAuthTokenResponse>('/api/v1/oauth/token', withSignal({
      method: 'POST',
      body: {
        grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
        device_code: input.deviceCode,
        code_verifier: input.codeVerifier,
        public_jwk: input.publicJwk,
      },
    }, signal)),
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
