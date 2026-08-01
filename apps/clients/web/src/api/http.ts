import { ApiError, createApiClient, type ApiRequestOptions } from '@termflow/client-core'
import { createBrowserHttpTransport } from '../adapters/browserHttpTransport'

export { ApiError }
export type { ApiErrorKind } from '@termflow/client-core'
export type { ApiRequestOptions }

export const browserApiClient = createApiClient(createBrowserHttpTransport())

export function apiRequest<T = void>(path: `/${string}`, options: ApiRequestOptions = {}): Promise<T> {
  return browserApiClient.request<T>(`/api/v1${path}`, options)
}
