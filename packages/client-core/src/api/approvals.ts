import type {
  ApprovalDecisionRequest,
  ApprovalDetailResponse,
  ApprovalListResponse,
} from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

function approvalPath(approvalId: string, suffix: string): `/${string}` {
  return `/api/v1/agent/approvals/${encodeURIComponent(approvalId)}${suffix}` as const
}

/**
 * Assisted-write approvals REST (termflow_control_plane.api.agent_approvals):
 * list/detail plus the two mutating actions. The B approval state machine
 * stays the source of truth; 409/410/404 error mapping happens in the UI
 * layer (M6b spec §4.7).
 */
export function createApprovalsApi(request: ApiRequest) {
  return {
    /** All approvals, optionally scoped to one conversation. */
    list: (options: { conversationId?: string, signal?: AbortSignal } = {}) => {
      const query = new URLSearchParams()
      if (options.conversationId !== undefined) query.set('conversation_id', options.conversationId)
      const suffix = query.size === 0 ? '' : `?${query.toString()}`
      return request<ApprovalListResponse>(
        `/api/v1/agent/approvals${suffix}`,
        withSignal({}, options.signal),
      )
    },

    detail: (approvalId: string, signal?: AbortSignal) =>
      request<ApprovalDetailResponse>(
        approvalPath(approvalId, ''),
        withSignal({}, signal),
      ),

    decide: (approvalId: string, decision: ApprovalDecisionRequest['decision'], signal?: AbortSignal) =>
      request<ApprovalDetailResponse>(
        approvalPath(approvalId, '/decide'),
        withSignal({ method: 'POST', body: { decision } }, signal),
      ),

    revoke: (approvalId: string, signal?: AbortSignal) =>
      request<ApprovalDetailResponse>(
        approvalPath(approvalId, '/revoke'),
        withSignal({ method: 'POST' }, signal),
      ),
  }
}

export type ApprovalsApi = ReturnType<typeof createApprovalsApi>
