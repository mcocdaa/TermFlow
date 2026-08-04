import { invoke } from '@tauri-apps/api/core'
import { fetch } from '@tauri-apps/plugin-http'
import { HttpTransportError, type HttpRequest, type HttpTransport } from '@termflow/client-core'
import { serverConfig } from '../serverConfig'
import { logNativeEvent } from '../diagnostics'

interface NativeHeaders { authorization: string; dpop: string }
const PUBLIC_PATHS = new Set(['/.well-known/oauth-authorization-server', '/healthz'])

function safePath(path: string) { return path.startsWith('/') && !path.startsWith('//') && !path.includes('://') && !path.includes('\\') }

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return typeof error === 'string' ? error : ''
}

function isHttpCapabilityFailure(error: unknown): boolean {
  const message = errorMessage(error).toLowerCase()
  return message.includes('error deserializing scope:')
    || message.includes('url not allowed on the configured scope:')
}

export function createTauriHttpTransport(): HttpTransport {
  return {
    async request(path: `/${string}`, request: HttpRequest) {
      if (!safePath(path)) throw new HttpTransportError('invalid_request')
      const url = new URL(path, `${serverConfig.current}/`).toString()
      const isPublic = PUBLIC_PATHS.has(path.split('?')[0] ?? path)
      const body = request.body === undefined ? undefined : JSON.stringify(request.body)
      const send = async (nonce?: string) => {
        const headers = new Headers({ accept: 'application/json', ...(request.headers ?? {}) })
        if (!isPublic) {
          const native = await invoke<NativeHeaders>('native_request_headers', {
            issuer: serverConfig.current,
            method: request.method,
            url,
            ...(nonce === undefined ? {} : { nonce }),
          })
          headers.set('authorization', native.authorization)
          headers.set('dpop', native.dpop)
        }
        const init: RequestInit = { method: request.method, headers }
        if (body !== undefined) { headers.set('content-type', 'application/json'); init.body = body }
        if (request.signal !== undefined) init.signal = request.signal
        return fetch(url, init)
      }
      const rememberNonce = async (response: Response) => {
        if (isPublic) return
        const nonce = response.headers.get('dpop-nonce')
        if (nonce !== null) {
          await invoke('native_remember_dpop_nonce', {
            issuer: serverConfig.current,
            nonce,
          })
        }
      }
      try {
        let response = await send()
        await rememberNonce(response)
        const nonce = response.status === 401 ? response.headers.get('dpop-nonce') : null
        if (!isPublic && nonce !== null) {
          response = await send(nonce)
          await rememberNonce(response)
        }
        let body: unknown
        if ((response.headers.get('content-type') ?? '').includes('application/json')) {
          try { body = await response.json() } catch { body = undefined }
        }
        void logNativeEvent({
          event: 'http_response',
          issuer: serverConfig.current,
          requestId: response.headers.get('x-request-id') ?? undefined,
          errorCode: response.status >= 400 ? `http_${response.status}` : undefined,
        })
        return { status: response.status, headers: response.headers, body }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw new HttpTransportError('aborted')
        if (isHttpCapabilityFailure(error)) throw new HttpTransportError('http_capability_denied')
        void logNativeEvent({ event: 'http_request_failed', issuer: serverConfig.current, level: 'error', errorCode: 'offline' })
        throw new HttpTransportError('offline')
      }
    },
  }
}
