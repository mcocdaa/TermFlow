import type { CleanupPendingResponse } from '@termflow/client-contracts'
import type { ApiRequestOptions, ApiRequestResponse } from '../http/types'
import { ApiError } from '../http/apiError'

export type DeleteResult =
  | { state: 'completed' }
  | { state: 'deletion_pending'; cleanupJobId: string; statusUrl: string }

export async function deleteResource(request: ApiRequestResponse, path: `/${string}`, options: ApiRequestOptions): Promise<DeleteResult> {
  const response = await request<CleanupPendingResponse>(path, { ...options, method: 'DELETE' })
  if (response.status === 204) return { state: 'completed' }
  const body = response.body
  if (response.status === 202 && body?.state === 'deletion_pending'
    && typeof body.cleanup_job_id === 'string' && typeof body.status_url === 'string') {
    return { state: 'deletion_pending', cleanupJobId: body.cleanup_job_id, statusUrl: body.status_url }
  }
  throw new ApiError('server')
}
