import type { PkcePair } from './pkce'
import type { AuthorizationBrowserPort, CredentialVaultPort, NativeAccessCredential, NativeClientDescriptor, NativeKeyPort } from './ports'

export interface AuthorizationExchangeRequest {
  issuer: string
  transaction: string
  verifier: string
  redirectUri: string
  key: NativeKeyPort
}

export interface NativeAuthorizationOptions {
  issuer: string
  authorizeEndpoint: string
  client: NativeClientDescriptor
  scopes: string[]
  browser: AuthorizationBrowserPort
  vault: CredentialVaultPort
  key: NativeKeyPort
  createPkce: () => Promise<PkcePair>
  createId: () => string
  exchange(request: AuthorizationExchangeRequest): Promise<NativeAccessCredential>
  redirectUri?: string
}

export class NativeAuthorizationSession {
  constructor(private readonly options: NativeAuthorizationOptions) {}

  async authorize(signal?: AbortSignal): Promise<NativeAccessCredential> {
    const state = this.options.createId()
    const pkce = await this.options.createPkce()
    const redirectUri = this.options.redirectUri ?? 'termflow://auth/callback'
    const url = new URL(this.options.authorizeEndpoint)
    url.searchParams.set('response_type', 'code')
    url.searchParams.set('redirect_uri', redirectUri)
    url.searchParams.set('state', state)
    url.searchParams.set('code_challenge', pkce.challenge)
    url.searchParams.set('code_challenge_method', pkce.method)
    url.searchParams.set('dpop_jkt', await this.options.key.thumbprint())
    url.searchParams.set('client_name', this.options.client.name)
    url.searchParams.set('platform', this.options.client.platform)
    url.searchParams.set('client_version', this.options.client.version)
    url.searchParams.set('public_jwk', JSON.stringify(await this.options.key.publicJwk()))
    for (const scope of this.options.scopes) url.searchParams.append('scopes', scope)
    const callbackPromise = this.options.browser.waitForCallback(state, signal)
    await this.options.browser.open(url.toString())

    const callback = new URL(await callbackPromise)
    const transaction = callback.searchParams.get('transaction_id')
    const callbackKeys = [...callback.searchParams.keys()]
    if (callback.protocol !== 'termflow:' || callback.hostname !== 'auth' || callback.pathname !== '/callback'
      || callback.searchParams.get('state') !== state
      || callbackKeys.length !== 2
      || callback.searchParams.getAll('state').length !== 1
      || callback.searchParams.getAll('transaction_id').length !== 1
      || !callbackKeys.every(key => key === 'state' || key === 'transaction_id')
      || transaction === null) {
      throw new Error('authorization_callback_invalid')
    }
    const credential = await this.options.exchange({
      issuer: this.options.issuer,
      transaction,
      verifier: pkce.verifier,
      redirectUri,
      key: this.options.key,
    })
    await this.options.vault.replace(this.options.issuer, credential)
    return credential
  }
}
