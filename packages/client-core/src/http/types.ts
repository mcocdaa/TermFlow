export interface HeaderReader {
  get(name: string): string | null
}

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

export interface HttpRequest {
  method: HttpMethod
  headers?: Readonly<Record<string, string>>
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
  headers?: Readonly<Record<string, string>>
  body?: unknown
  signal?: AbortSignal
}

export type ApiRequest = <T = void>(path: `/${string}`, options?: ApiRequestOptions) => Promise<T>

export interface ApiResponseHeaders {
  dpopNonce?: string
  retryAfter?: string
}

export interface ApiResponse<T> {
  status: number
  headers: ApiResponseHeaders
  body: T
}

export type ApiRequestResponse = <T = void>(path: `/${string}`, options?: ApiRequestOptions) => Promise<ApiResponse<T>>

export type HttpTransportErrorKind = 'aborted' | 'offline' | 'invalid_request' | 'http_capability_denied'

export class HttpTransportError extends Error {
  constructor(public readonly kind: HttpTransportErrorKind) {
    super(kind)
    this.name = 'HttpTransportError'
  }
}
