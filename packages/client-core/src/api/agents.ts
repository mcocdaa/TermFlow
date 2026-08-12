import type {
  AgentCapabilitiesResponse,
  AgentConversationCreateRequest,
  AgentConversationDetailResponse,
  AgentConversationListResponse,
  AgentConversationResponse,
  AgentEventListResponse,
  AgentEventResponse,
  AgentMessageListResponse,
} from '@termflow/client-contracts'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

function withSignal(options: ApiRequestOptions, signal: AbortSignal | undefined): ApiRequestOptions {
  if (signal !== undefined) options.signal = signal
  return options
}

function conversationPath(conversationId: string, suffix: string): `/${string}` {
  return `/api/v1/agent/conversations/${encodeURIComponent(conversationId)}${suffix}` as const
}

export function createAgentsApi(request: ApiRequest) {
  return {
    capabilities: (signal?: AbortSignal) =>
      request<AgentCapabilitiesResponse>('/api/v1/agent/capabilities', withSignal({}, signal)),

    listConversations: (signal?: AbortSignal) =>
      request<AgentConversationListResponse>('/api/v1/agent/conversations', withSignal({}, signal)),

    createConversation: (body: AgentConversationCreateRequest, signal?: AbortSignal) =>
      request<AgentConversationResponse>(
        '/api/v1/agent/conversations',
        withSignal({ method: 'POST', body }, signal),
      ),

    getConversation: (conversationId: string, signal?: AbortSignal) =>
      request<AgentConversationDetailResponse>(
        conversationPath(conversationId, ''),
        withSignal({}, signal),
      ),

    listMessages: (conversationId: string, options: { limit?: number, offset?: number, signal?: AbortSignal } = {}) => {
      const query = new URLSearchParams()
      if (options.limit !== undefined) query.set('limit', String(options.limit))
      if (options.offset !== undefined) query.set('offset', String(options.offset))
      const suffix = query.size === 0 ? '' : `?${query.toString()}`
      return request<AgentMessageListResponse>(
        conversationPath(conversationId, `/messages${suffix}`),
        withSignal({}, options.signal),
      )
    },

    listEvents: (conversationId: string, options: { since?: number, limit?: number, offset?: number, signal?: AbortSignal } = {}) => {
      const query = new URLSearchParams()
      if (options.since !== undefined) query.set('since', String(options.since))
      if (options.limit !== undefined) query.set('limit', String(options.limit))
      if (options.offset !== undefined) query.set('offset', String(options.offset))
      const suffix = query.size === 0 ? '' : `?${query.toString()}`
      return request<AgentEventListResponse>(
        conversationPath(conversationId, `/events${suffix}`),
        withSignal({}, options.signal),
      )
    },
  }
}

export type AgentsApi = ReturnType<typeof createAgentsApi>

/**
 * Page the conversation's canonical events after ``sinceSeq`` in
 * ``database_seq`` order. The endpoint returns at most 200 events per page
 * and a ``next_cursor`` that advances only while events remain; replay stops
 * when a page is empty or the cursor stops advancing.
 */
export async function fetchEventsSince(
  request: ApiRequest,
  conversationId: string,
  sinceSeq: number,
): Promise<AgentEventResponse[]> {
  const events: AgentEventResponse[] = []
  let cursor = sinceSeq
  for (;;) {
    const page = await request<AgentEventListResponse>(
      `/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/events?since=${cursor}&limit=200`,
    )
    events.push(...page.events)
    if (page.events.length === 0) return events
    if (page.next_cursor === null || page.next_cursor <= cursor) return events
    cursor = page.next_cursor
  }
}
