import { invoke } from '@tauri-apps/api/core'
import { HttpTransportError, type HeaderReader, type HttpRequest, type HttpTransport } from '@termflow/client-core'
import { serverConfig } from '../serverConfig'
import { logNativeEvent, sanitizeNativeDetail } from '../diagnostics'

interface NativeHttpResponse { status: number; headers: Record<string, string>; body: unknown }

function errorMessage(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return typeof error === 'string' ? error : ''
}

function reader(headers: Record<string, string>): HeaderReader {
  return { get: (name: string) => headers[name.toLowerCase()] ?? null }
}

export function createTauriHttpTransport(): HttpTransport {
  return {
    async request(path: `/${string}`, request: HttpRequest) {
      const body = request.body === undefined ? undefined : JSON.parse(JSON.stringify(request.body))
      const headers: Record<string, string> = {}
      for (const [name, value] of Object.entries(request.headers ?? {})) headers[name] = value
      const send = async (nonce?: string) => invoke<NativeHttpResponse>('native_http_request', {
        issuer: serverConfig.current,
        path,
        method: request.method,
        ...(Object.keys(headers).length > 0 ? { headers } : {}),
        ...(body === undefined ? {} : { body }),
        ...(nonce === undefined ? {} : { nonce }),
      })
      try {
        let response = await send()
        if (response.status === 401 && response.headers['dpop-nonce'] !== undefined) {
          response = await send(response.headers['dpop-nonce'])
        }
        void logNativeEvent({
          event: 'http_response',
          issuer: serverConfig.current,
          requestId: response.headers['x-request-id'] ?? undefined,
          errorCode: response.status >= 400 ? `http_${response.status}` : undefined,
        })
        return { status: response.status, headers: reader(response.headers), body: response.body }
      } catch (error) {
        if (errorMessage(error).includes('url_not_allowed') || errorMessage(error).includes('method_not_allowed')) {
          void logNativeEvent({ event: 'http_request_failed', issuer: serverConfig.current, level: 'error', errorCode: 'http_capability_denied', errorDetail: sanitizeNativeDetail(error) })
          throw new HttpTransportError('http_capability_denied')
        }
        void logNativeEvent({ event: 'http_request_failed', issuer: serverConfig.current, level: 'error', errorCode: 'offline', errorDetail: sanitizeNativeDetail(error) })
        throw new HttpTransportError('offline')
      }
    },
  }
}
