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
    url.searchParams.set('client_platform', this.options.client.platform)
    url.searchParams.set('client_version', this.options.client.version)
    url.searchParams.set('scope', this.options.scopes.join(' '))
    await this.options.browser.open(url.toString())

    const callback = new URL(await this.options.browser.waitForCallback(state, signal))
    const transaction = callback.searchParams.get('transaction')
    if (callback.protocol !== 'termflow:' || callback.hostname !== 'auth' || callback.pathname !== '/callback'
      || callback.searchParams.get('state') !== state || callback.searchParams.get('issuer') !== this.options.issuer
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
