import { invoke } from '@tauri-apps/api/core'
import { onOpenUrl } from '@tauri-apps/plugin-deep-link'
import { openUrl } from '@tauri-apps/plugin-opener'
import type { AuthorizationBrowserPort, NativeAccessCredential, NativeKeyPort, PublicEcJwk } from '@termflow/client-core'

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
    await openUrl(url)
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
      if (match !== undefined) { dispose(); resolve(match) }
    }).then((listener) => { if (disposed) listener(); else unlisten = listener })
    pendingCallback = current
    void current.ready.catch((error: unknown) => { dispose(); reject(error) })
  }),
}

export async function exchangeAuthorization(input: { issuer: string; transaction: string; verifier: string; redirectUri: string }): Promise<NativeAccessCredential> {
  return invoke<NativeAccessCredential>('native_exchange_authorization', {
    issuer: input.issuer, transactionId: input.transaction, codeVerifier: input.verifier, redirectUri: input.redirectUri,
  })
}

export async function refreshAuthorization(issuer: string): Promise<NativeAccessCredential> {
  return invoke<NativeAccessCredential>('native_refresh_access', { issuer })
}
