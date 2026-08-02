import type {
  ErrorEnvelope,
} from '@termflow/client-contracts'
import { createComputersApi } from '../api/computers'
import { createClientsApi } from '../api/clients'
import { createDashboardApi } from '../api/dashboard'
import { createOAuthApi } from '../api/oauth'
import { createSecurityApi } from '../api/security'
import { createSessionApi } from '../api/session'
import { createTermsApi } from '../api/terms'
import { ApiError, type ApiErrorKind } from './apiError'
import { HttpTransportError, type ApiRequestOptions, type ApiResponse, type HttpMethod, type HttpRequest, type HttpResponse, type HttpTransport } from './types'

function kindForStatus(status: number): ApiErrorKind {
  if (status === 401 || status === 403) return 'authentication'
  if (status === 400 || status === 404 || status === 409 || status === 422) return 'validation'
  if (status === 429) return 'rate_limit'
  return 'server'
}

function publicErrorDetails(body: unknown): { code?: string, requestId?: string } {
  if (typeof body !== 'object' || body === null) return {}
  const error = (body as Partial<ErrorEnvelope>).error
  if (typeof error !== 'object' || error === null) return {}
  const result: { code?: string, requestId?: string } = {}
  if (typeof error.code === 'string') result.code = error.code
  if (typeof error.request_id === 'string') result.requestId = error.request_id
  return result
}

function transportFailure(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  if (error instanceof HttpTransportError) {
    if (error.kind === 'aborted') return new ApiError('aborted')
    if (error.kind === 'invalid_request') return new ApiError('validation')
  }
  return new ApiError('offline')
}

function httpRequest(method: HttpMethod, headers: Readonly<Record<string, string>> | undefined, body: unknown, signal: AbortSignal | undefined): HttpRequest {
  const request: HttpRequest = { method }
  if (headers !== undefined) request.headers = headers
  if (body !== undefined) request.body = body
  if (signal !== undefined) request.signal = signal
  return request
}

export function createApiClient(transport: HttpTransport) {
  async function requestTransport(path: `/${string}`, options: ApiRequestOptions): Promise<HttpResponse> {
    let response
    try {
      response = await transport.request(path, httpRequest(options.method ?? 'GET', options.headers, options.body, options.signal))
    } catch (error) {
      throw transportFailure(error)
    }

    if (response.status < 200 || response.status >= 300) {
      const details = publicErrorDetails(response.body)
      const retryAfter = response.headers.get('retry-after')
      const dpopNonce = response.headers.get('dpop-nonce')
      const parsedRetryAfter = retryAfter === null ? undefined : Number.parseInt(retryAfter, 10)
      throw new ApiError(kindForStatus(response.status), {
        status: response.status,
        ...details,
        ...(parsedRetryAfter !== undefined && Number.isFinite(parsedRetryAfter) ? { retryAfterSeconds: parsedRetryAfter } : {}),
        ...(dpopNonce === null ? {} : { dpopNonce }),
      })
    }
    return response
  }

  async function requestResponse<T = void>(path: `/${string}`, options: ApiRequestOptions = {}): Promise<ApiResponse<T>> {
    const response = await requestTransport(path, options)
    const dpopNonce = response.headers.get('dpop-nonce')
    const retryAfter = response.headers.get('retry-after')
    return {
      status: response.status,
      headers: {
        ...(dpopNonce === null ? {} : { dpopNonce }),
        ...(retryAfter === null ? {} : { retryAfter }),
      },
      body: (response.status === 204 ? undefined : response.body) as T,
    }
  }

  async function request<T = void>(path: `/${string}`, options: ApiRequestOptions = {}): Promise<T> {
    const response = await requestResponse<T>(path, options)
    if (response.status === 204) return undefined as T
    return response.body as T
  }

  return {
    request,
    requestResponse,
    sessions: createSessionApi(request),
    dashboard: createDashboardApi(request),
    computers: createComputersApi(request),
    security: createSecurityApi(request),
    oauth: createOAuthApi(request),
    clients: createClientsApi(request),
    terms: createTermsApi(request),
  }
}

export type ApiClient = ReturnType<typeof createApiClient>
