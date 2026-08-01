import { HttpTransportError, type HttpRequest, type HttpTransport } from '@termflow/client-core'

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

function isSafeRelativePath(path: string): boolean {
  return path.startsWith('/') && !path.startsWith('//') && !path.includes('://') && !path.includes('\\')
}

const browserFetch: Fetch = (input, init) => globalThis.fetch(input, init)

export function createBrowserHttpTransport(fetchImplementation: Fetch = browserFetch): HttpTransport {
  return {
    async request(path: `/${string}`, request: HttpRequest) {
      if (!isSafeRelativePath(path)) throw new HttpTransportError('invalid_request')

      const headers = new Headers({ accept: 'application/json' })
      const init: RequestInit = { credentials: 'same-origin', headers }
      if (request.method !== 'GET') init.method = request.method
      if (request.body !== undefined) {
        headers.set('content-type', 'application/json')
        init.body = JSON.stringify(request.body)
      }
      if (request.signal !== undefined) init.signal = request.signal

      try {
        const response = await fetchImplementation(path, init)
        const contentType = response.headers.get('content-type') ?? ''
        let body: unknown
        if (contentType.includes('application/json')) {
          try {
            body = await response.json()
          } catch {
            body = undefined
          }
        }
        return { status: response.status, headers: response.headers, body }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          throw new HttpTransportError('aborted')
        }
        throw new HttpTransportError('offline')
      }
    },
  }
}
