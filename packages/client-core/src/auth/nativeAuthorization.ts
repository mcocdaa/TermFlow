import type { PkcePair } from './pkce'
import { createAuthorizationStateMachine } from './authorizationState'
import type { AuthorizationBrowserPort, AuthorizationStateListener, CredentialVaultPort, NativeAccessCredential, NativeClientDescriptor, NativeKeyPort } from './ports'

const transactionIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Structurally validate an app-scheme callback without knowing the expected state. */
export function parseNativeAuthorizationCallback(value: string): { state: string; transaction: string } | null {
  try {
    const callback = new URL(value)
    const keys = [...callback.searchParams.keys()]
    const transaction = callback.searchParams.get('transaction_id')
    const state = callback.searchParams.get('state')
    if (
      callback.protocol !== 'termflow:'
      || callback.username !== ''
      || callback.password !== ''
      || callback.hostname !== 'auth'
      || callback.port !== ''
      || callback.pathname !== '/callback'
      || callback.hash !== ''
      || state === null
      || callback.searchParams.getAll('state').length !== 1
      || callback.searchParams.getAll('transaction_id').length !== 1
      || transaction === null
      || !transactionIdPattern.test(transaction)
      || keys.length !== 2
      || keys.some(key => key !== 'state' && key !== 'transaction_id')
    ) {
      return null
    }
    return { state, transaction }
  } catch {
    return null
  }
}

export function isValidNativeAuthorizationCallback(value: string, state: string): boolean {
  return parseNativeAuthorizationCallback(value)?.state === state
}

export interface AuthorizationExchangeRequest {
  issuer: string
  transaction: string
  verifier: string
  redirectUri: string
  key: NativeKeyPort
}

export interface NativeAuthorizationOptions extends AuthorizationStateListener {
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
    const progress = createAuthorizationStateMachine({ onState: this.options.onState })
    progress.requesting()
    if (signal?.aborted) {
      progress.cancelled()
      throw new Error('authorization_cancelled')
    }
    try {
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
      const callbackAbort = new AbortController()
      const cancelCallback = () => callbackAbort.abort()
      signal?.addEventListener('abort', cancelCallback, { once: true })
      const callbackPromise = this.options.browser
        .waitForCallback(state, callbackAbort.signal)
        .finally(() => signal?.removeEventListener('abort', cancelCallback))
      try {
        if (callbackAbort.signal.aborted) await callbackPromise
        await this.options.browser.open(url.toString())
      } catch (error) {
        cancelCallback()
        // The adapter owns the native listener. Ensure a failed browser launch
        // releases it before surfacing the launch failure to the view.
        await callbackPromise.catch(() => undefined)
        throw error
      }
      progress.pending()

      const callbackValue = await callbackPromise
      if (!isValidNativeAuthorizationCallback(callbackValue, state)) {
        throw new Error('authorization_callback_invalid')
      }
      const callback = new URL(callbackValue)
      const transaction = callback.searchParams.get('transaction_id')
      if (transaction === null) {
        throw new Error('authorization_callback_invalid')
      }
      progress.approved()
      const credential = await this.options.exchange({
        issuer: this.options.issuer,
        transaction,
        verifier: pkce.verifier,
        redirectUri,
        key: this.options.key,
      })
      await this.options.vault.replace(this.options.issuer, credential)
      progress.connected()
      return credential
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || (error as Error)?.message === 'authorization_cancelled') progress.cancelled()
      else progress.failed()
      throw error
    }
  }
}
