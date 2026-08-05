import { invoke } from '@tauri-apps/api/core'
import { onOpenUrl } from '@tauri-apps/plugin-deep-link'
import { openUrl } from '@tauri-apps/plugin-opener'
import type { AuthorizationBrowserPort, NativeAccessCredential, NativeKeyPort, PublicEcJwk } from '@termflow/client-core'
import type { OAuthTokenResponse } from '@termflow/client-contracts'
import { logNativeEvent, sanitizeNativeDetail } from '../diagnostics'

export function createTauriKey(issuer: string): NativeKeyPort {
  return {
    publicJwk: () => invoke<PublicEcJwk>('native_public_jwk', { issuer }),
    thumbprint: () => invoke<string>('native_key_thumbprint', { issuer }),
    signJwt: async (input) => new Uint8Array(await invoke<number[]>('native_sign_jwt', { issuer, signingInput: Array.from(input) })),
  }
}

let pendingCallback: { ready: Promise<void> } | undefined

export const tauriAuthorizationBrowser: AuthorizationBrowserPort = {
  open: async (url) => {
    const pending = pendingCallback
    if (pending === undefined) throw new Error('authorization_listener_missing')
    await pending.ready
    void logNativeEvent({ event: 'browser_open_started', issuer: new URL(url).origin })
    try { await openUrl(url) } catch (error) {
      void logNativeEvent({ event: 'browser_open_failed', issuer: new URL(url).origin, level: 'error', errorCode: 'browser_open_failed', errorDetail: sanitizeNativeDetail(error) })
      throw error
    }
  },
  waitForCallback: (state, signal) => new Promise((resolve, reject) => {
    let disposed = false
    let unlisten: (() => void) | undefined
    const current = { ready: Promise.resolve() }
    const dispose = () => {
      disposed = true
      unlisten?.()
      if (pendingCallback === current) pendingCallback = undefined
    }
    signal?.addEventListener('abort', () => { dispose(); reject(new Error('authorization_cancelled')) }, { once: true })
    current.ready = onOpenUrl((urls) => {
      if (disposed) return
      const match = urls.find((value) => {
        try { return new URL(value).searchParams.get('state') === state } catch { return false }
      })
      if (match !== undefined) {
        dispose()
        void logNativeEvent({ event: 'authorization_callback_received', issuer: new URL(match).origin })
        resolve(match)
      }
    }).then((listener) => { if (disposed) listener(); else unlisten = listener })
    pendingCallback = current
    void current.ready.catch((error: unknown) => {
      dispose()
      void logNativeEvent({ event: 'authorization_callback_listener_failed', level: 'error', errorCode: 'authorization_listener_failed', errorDetail: sanitizeNativeDetail(error) })
      reject(error)
    })
  }),
}

export async function exchangeAuthorization(input: { issuer: string; transaction: string; verifier: string; redirectUri: string }): Promise<NativeAccessCredential> {
  try {
    return await invoke<NativeAccessCredential>('native_exchange_authorization', {
      issuer: input.issuer, transactionId: input.transaction, codeVerifier: input.verifier, redirectUri: input.redirectUri,
    })
  } catch (error) {
    void logNativeEvent({ event: 'token_exchange_failed', issuer: input.issuer, level: 'error', errorCode: 'token_exchange_failed', errorDetail: sanitizeNativeDetail(error) })
    throw error
  }
}

/** Device-code exchange runs in Rust so it can sign DPoP before a credential exists. */
export function pollDeviceAuthorization(input: { issuer: string; deviceCode: string; codeVerifier: string; publicJwk: PublicEcJwk }, signal?: AbortSignal): Promise<OAuthTokenResponse> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  return invoke<OAuthTokenResponse>('native_exchange_device_code', {
    issuer: input.issuer,
    deviceCode: input.deviceCode,
    codeVerifier: input.codeVerifier,
    publicJwk: input.publicJwk,
  }).catch((error) => {
    if ((error as Error)?.name !== 'AbortError') {
      void logNativeEvent({ event: 'device_token_exchange_failed', issuer: input.issuer, level: 'error', errorCode: 'device_token_exchange_failed', errorDetail: sanitizeNativeDetail(error) })
    }
    throw error
  })
}

export async function refreshAuthorization(issuer: string): Promise<NativeAccessCredential> {
  return invoke<NativeAccessCredential>('native_refresh_access', { issuer })
}
