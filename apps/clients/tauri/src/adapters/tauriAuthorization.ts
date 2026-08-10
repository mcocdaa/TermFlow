import { invoke } from '@tauri-apps/api/core'
import { onOpenUrl } from '@tauri-apps/plugin-deep-link'
import { openUrl } from '@tauri-apps/plugin-opener'
import { isValidNativeAuthorizationCallback } from '@termflow/client-core'
import type { AuthorizationBrowserPort, NativeAccessCredential, NativeKeyPort, PublicEcJwk } from '@termflow/client-core'
import { logNativeEvent, sanitizeNativeDetail } from '../diagnostics'

const LOOPBACK_PORT_MIN = 49152
const LOOPBACK_PORT_MAX = 65535

export function createTauriKey(issuer: string): NativeKeyPort {
  return {
    publicJwk: () => invoke<PublicEcJwk>('native_public_jwk', { issuer }),
    thumbprint: () => invoke<string>('native_key_thumbprint', { issuer }),
    signJwt: async (input) => new Uint8Array(await invoke<number[]>('native_sign_jwt', { issuer, signingInput: Array.from(input) })),
  }
}

let pendingCallback: { ready: Promise<void> } | undefined

/** Aligned with the server transaction TTL so a dead approval never hangs forever. */
const CALLBACK_TIMEOUT_MILLIS = 5 * 60 * 1000

function carriesState(value: string, state: string): boolean {
  try { return new URL(value).searchParams.getAll('state').includes(state) } catch { return false }
}

/** App-scheme (termflow://) deep-link callback, used by mobile and as the fallback. */
function deepLinkWaitForCallback(state: string, signal?: AbortSignal): Promise<string> {
  if (signal?.aborted) return Promise.reject(new Error('authorization_cancelled'))
  return new Promise((resolve, reject) => {
    let disposed = false
    let unlisten: (() => void) | undefined
    let timer: ReturnType<typeof globalThis.setTimeout> | undefined
    const current = { ready: Promise.resolve() }
    const dispose = () => {
      disposed = true
      unlisten?.()
      if (pendingCallback === current) pendingCallback = undefined
      if (timer !== undefined) { globalThis.clearTimeout(timer); timer = undefined }
    }
    signal?.addEventListener('abort', () => { dispose(); reject(new Error('authorization_cancelled')) }, { once: true })
    current.ready = onOpenUrl((urls) => {
      if (disposed) return
      const match = urls.find((value) => isValidNativeAuthorizationCallback(value, state))
      if (match !== undefined) {
        dispose()
        void logNativeEvent({ event: 'authorization_callback_received' })
        resolve(match)
      } else if (urls.some((value) => carriesState(value, state))) {
        void logNativeEvent({ event: 'authorization_callback_invalid', level: 'warn', errorCode: 'authorization_callback_invalid' })
      }
    }).then((listener) => { if (disposed) listener(); else unlisten = listener })
    pendingCallback = current
    timer = globalThis.setTimeout(() => {
      dispose()
      void logNativeEvent({ event: 'authorization_callback_timeout', level: 'warn', errorCode: 'authorization_callback_timeout' })
      reject(new Error('authorization_callback_timeout'))
    }, CALLBACK_TIMEOUT_MILLIS)
    void current.ready.catch((error: unknown) => {
      dispose()
      void logNativeEvent({ event: 'authorization_callback_listener_failed', level: 'error', errorCode: 'authorization_listener_failed', errorDetail: sanitizeNativeDetail(error) })
      reject(error)
    })
  })
}

async function deepLinkEnsureReady(): Promise<void> {
  const pending = pendingCallback
  if (pending === undefined) throw new Error('authorization_listener_missing')
  await pending.ready
}

/** Loopback HTTP callback: the browser hits http://127.0.0.1:<port> after approval. */
function loopbackWaitForCallback(state: string, signal?: AbortSignal): Promise<string> {
  if (signal?.aborted) return Promise.reject(new Error('authorization_cancelled'))
  return new Promise((resolve, reject) => {
    let settled = false
    const fail = (message: string) => {
      if (settled) return
      settled = true
      reject(new Error(message))
    }
    void invoke<string>('native_wait_authorization_callback', { expectedState: state })
      .then((value) => { if (!settled) { settled = true; resolve(value) } })
      .catch(() => fail('authorization_callback_timeout'))
    const onAbort = () => {
      if (settled) return
      settled = true
      void invoke('native_cancel_authorization_listener', { expectedState: state }).catch(() => undefined)
      reject(new Error('authorization_cancelled'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export interface TauriAuthorizationBrowserOptions {
  /** Canonical server origin, used to return the browser to the web home page. */
  issuer: string
  /** Desktop clients hand the browser back over a loopback HTTP callback. */
  loopback: boolean
}

export function tauriAuthorizationBrowser(options: TauriAuthorizationBrowserOptions): AuthorizationBrowserPort {
  let boundState: string | null = null
  return {
    prepareCallback: async (state) => {
      if (!options.loopback) return undefined
      try {
        const port = await invoke<number>('native_bind_authorization_listener', { expectedState: state, issuer: options.issuer })
        if (port < LOOPBACK_PORT_MIN || port > LOOPBACK_PORT_MAX) return undefined
        boundState = state
        return `http://127.0.0.1:${port}/oauth/callback`
      } catch (error) {
        void logNativeEvent({ event: 'loopback_listener_bind_failed', issuer: options.issuer, level: 'warn', errorCode: 'loopback_listener_bind_failed', errorDetail: sanitizeNativeDetail(error) })
        boundState = null
        return undefined
      }
    },
    open: async (url) => {
      const parsed = new URL(url)
      if (boundState === null || boundState !== parsed.searchParams.get('state')) await deepLinkEnsureReady()
      void logNativeEvent({ event: 'browser_open_started', issuer: parsed.origin })
      try { await openUrl(url) } catch (error) {
        void logNativeEvent({ event: 'browser_open_failed', issuer: parsed.origin, level: 'error', errorCode: 'browser_open_failed', errorDetail: sanitizeNativeDetail(error) })
        throw error
      }
    },
    waitForCallback: (state, signal) => {
      if (boundState === state) {
        const promise = loopbackWaitForCallback(state, signal)
        void promise.finally(() => { if (boundState === state) boundState = null }).catch(() => undefined)
        return promise
      }
      return deepLinkWaitForCallback(state, signal)
    },
  }
}

export async function exchangeAuthorization(input: { issuer: string; transaction: string; verifier: string; redirectUri: string }): Promise<NativeAccessCredential> {
  try {
    return await invoke<NativeAccessCredential>('native_exchange_authorization', {
      request: {
        issuer: input.issuer, transactionId: input.transaction, codeVerifier: input.verifier, redirectUri: input.redirectUri,
      },
    })
  } catch (error) {
    void logNativeEvent({ event: 'token_exchange_failed', issuer: input.issuer, level: 'error', errorCode: 'token_exchange_failed', errorDetail: sanitizeNativeDetail(error) })
    throw error
  }
}

/** Device-code exchange runs in Rust so it can sign DPoP before a credential exists. */
export function pollDeviceAuthorization(input: { issuer: string; deviceCode: string; codeVerifier: string; publicJwk: PublicEcJwk }, signal?: AbortSignal): Promise<NativeAccessCredential> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  return invoke<NativeAccessCredential>('native_exchange_device_code', {
    request: {
      issuer: input.issuer,
      deviceCode: input.deviceCode,
      codeVerifier: input.codeVerifier,
      publicJwk: input.publicJwk,
    },
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
