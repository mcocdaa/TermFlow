export interface HeaderReader {
  get(name: string): string | null
}

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

export interface HttpRequest {
  method: HttpMethod
  body?: unknown
  signal?: AbortSignal
}

export interface HttpResponse {
  status: number
  headers: HeaderReader
  body: unknown
}

export interface HttpTransport {
  request(path: `/${string}`, request: HttpRequest): Promise<HttpResponse>
}

export interface ApiRequestOptions {
  method?: HttpMethod
  body?: unknown
  signal?: AbortSignal
}

export type ApiRequest = <T = void>(path: `/${string}`, options?: ApiRequestOptions) => Promise<T>

export type HttpTransportErrorKind = 'aborted' | 'offline' | 'invalid_request'

export class HttpTransportError extends Error {
  constructor(public readonly kind: HttpTransportErrorKind) {
    super(kind)
    this.name = 'HttpTransportError'
  }
}
