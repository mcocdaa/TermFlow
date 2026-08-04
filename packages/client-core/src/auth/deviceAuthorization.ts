import type { OAuthDeviceTokenErrorCode, OAuthPublicJwk, OAuthTokenResponse } from '@termflow/client-contracts'
import type { DeviceAuthorizationPollInput } from '../api/oauth'
import type { CredentialVaultPort, NativeAccessCredential } from './ports'

export interface DeviceAuthorizationSessionOptions {
  issuer: string
  deviceCode: string
  codeVerifier: string
  publicJwk: OAuthPublicJwk
  /** The server-provided interval, in seconds. */
  interval?: number
  /** Alias accepted when callers use the RFC terminology. */
  intervalSeconds?: number
  poll: (input: DeviceAuthorizationPollInput, signal?: AbortSignal) => Promise<OAuthTokenResponse>
  vault: CredentialVaultPort
  /** Injected in tests; the default waits in milliseconds and observes abort. */
  sleep?: (milliseconds: number, signal?: AbortSignal) => Promise<void>
  now?: () => number
}

const DEVICE_POLL_GRANT_ERRORS: ReadonlySet<OAuthDeviceTokenErrorCode> = new Set([
  'authorization_pending', 'slow_down', 'access_denied', 'expired_token',
])

function errorCode(error: unknown): string | undefined {
  if (typeof error === 'string') return error
  if (typeof error !== 'object' || error === null) return undefined
  const value = error as { code?: unknown, error?: unknown }
  if (typeof value.code === 'string') return value.code
  if (typeof value.error === 'string') return value.error
  if (typeof value.error === 'object' && value.error !== null && 'code' in value.error) {
    const nested = (value.error as { code?: unknown }).code
    if (typeof nested === 'string') return nested
  }
  return undefined
}

function abortError(): Error {
  const error = new Error('The device authorization was cancelled.')
  error.name = 'AbortError'
  return error
}

function defaultSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, milliseconds)
    const onAbort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      reject(abortError())
    }
    function done() {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

function asCredential(response: OAuthTokenResponse, now: () => number): NativeAccessCredential {
  return {
    accessToken: response.access_token,
    expiresAt: new Date(now() + response.expires_in * 1000).toISOString(),
    tokenType: response.token_type,
  }
}

/**
 * Polls an OAuth device grant without owning transport, timing, or storage.
 * Device code and PKCE material remain in this session's memory only.
 */
export class DeviceAuthorizationSession {
  private readonly cancellation = new AbortController()
  private readonly sleep: (milliseconds: number, signal: AbortSignal) => Promise<void>
  private readonly now: () => number
  private cancelled = false

  constructor(private readonly options: DeviceAuthorizationSessionOptions) {
    const injectedSleep = options.sleep
    this.sleep = injectedSleep === undefined
      ? defaultSleep
      : (milliseconds, signal) => this.waitWithAbort(injectedSleep(milliseconds), signal)
    this.now = options.now ?? (() => Date.now())
  }

  cancel(): void {
    this.cancelled = true
    this.cancellation.abort()
  }

  /** Returns the wire OAuth response and stores a native short-lived credential on success. */
  async poll(signal?: AbortSignal): Promise<OAuthTokenResponse> {
    const combined = this.combinedSignal(signal)
    let interval = this.initialInterval()
    const input: DeviceAuthorizationPollInput = {
      deviceCode: this.options.deviceCode,
      codeVerifier: this.options.codeVerifier,
      publicJwk: this.options.publicJwk,
    }

    while (true) {
      this.throwIfAborted(combined)
      await this.sleep(interval * 1000, combined)
      this.throwIfAborted(combined)
      try {
        // Race the transport with cancellation as well as passing the signal
        // through; adapters should abort their request, but a pure injected
        // test transport is not required to implement AbortSignal semantics.
        const response = await this.waitWithAbort(this.options.poll(input, combined), combined)
        this.throwIfAborted(combined)
        await this.options.vault.replace(this.options.issuer, asCredential(response, this.now))
        return response
      } catch (error) {
        this.throwIfAborted(combined)
        const code = errorCode(error)
        if (!DEVICE_POLL_GRANT_ERRORS.has(code as OAuthDeviceTokenErrorCode)) throw error
        if (code === 'authorization_pending') continue
        if (code === 'slow_down') {
          interval += 5
          continue
        }
        // access_denied and expired_token are terminal and are deliberately
        // rethrown so callers can present an actionable state.
        throw error
      }
    }
  }

  /** Returns the credential shape used by NativeAuthorizationSession/TokenSession. */
  async authorize(signal?: AbortSignal): Promise<NativeAccessCredential> {
    const response = await this.poll(signal)
    return asCredential(response, this.now)
  }

  private initialInterval(): number {
    const value = this.options.intervalSeconds ?? this.options.interval ?? 5
    return Math.max(0, value)
  }

  private combinedSignal(external?: AbortSignal): AbortSignal {
    const controller = new AbortController()
    const abort = () => controller.abort()
    if (this.cancelled || this.cancellation.signal.aborted || external?.aborted) controller.abort()
    else {
      this.cancellation.signal.addEventListener('abort', abort, { once: true })
      external?.addEventListener('abort', abort, { once: true })
    }
    return controller.signal
  }

  private throwIfAborted(signal: AbortSignal): void {
    if (signal.aborted) throw abortError()
  }

  private waitWithAbort<T>(wait: Promise<T>, signal: AbortSignal): Promise<T> {
    if (signal.aborted) return Promise.reject(abortError())
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        signal.removeEventListener('abort', onAbort)
        reject(abortError())
      }
      signal.addEventListener('abort', onAbort, { once: true })
      wait.then((value) => {
        signal.removeEventListener('abort', onAbort)
        if (signal.aborted) reject(abortError())
        else resolve(value)
      }, (error) => {
        signal.removeEventListener('abort', onAbort)
        reject(error)
      })
    })
  }
}

export function createDeviceAuthorizationSession(options: DeviceAuthorizationSessionOptions): DeviceAuthorizationSession {
  return new DeviceAuthorizationSession(options)
}
