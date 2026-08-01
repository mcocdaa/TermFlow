import type {
  ErrorEnvelope,
} from '@termflow/client-contracts'
import { createComputersApi } from '../api/computers'
import { createDashboardApi } from '../api/dashboard'
import { createSessionApi } from '../api/session'
import { createTermsApi } from '../api/terms'
import { ApiError, type ApiErrorKind } from './apiError'
import { HttpTransportError, type ApiRequestOptions, type HttpMethod, type HttpRequest, type HttpTransport } from './types'

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

function httpRequest(method: HttpMethod, body: unknown, signal: AbortSignal | undefined): HttpRequest {
  const request: HttpRequest = { method }
  if (body !== undefined) request.body = body
  if (signal !== undefined) request.signal = signal
  return request
}

export function createApiClient(transport: HttpTransport) {
  async function request<T = void>(path: `/${string}`, options: ApiRequestOptions = {}): Promise<T> {
    let response
    try {
      response = await transport.request(path, httpRequest(options.method ?? 'GET', options.body, options.signal))
    } catch (error) {
      throw transportFailure(error)
    }

    if (response.status < 200 || response.status >= 300) {
      const details = publicErrorDetails(response.body)
      throw new ApiError(kindForStatus(response.status), { status: response.status, ...details })
    }
    if (response.status === 204) return undefined as T
    return response.body as T
  }

  return {
    request,
    sessions: createSessionApi(request),
    dashboard: createDashboardApi(request),
    computers: createComputersApi(request),
    terms: createTermsApi(request),
  }
}

export type ApiClient = ReturnType<typeof createApiClient>
