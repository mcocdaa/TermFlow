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
import { parseAguiEvent, type AguiEvent, type AguiReplayBatch } from '../agent/agui'
import type { ApiRequest, ApiRequestOptions } from '../http/types'

//: Hand-written M4.5 admission models (termflow_control_plane.api.
//: agent_conversations). They move to generated.ts with the contracts
//: regeneration task; this module must not depend on their presence there.
export interface AgentSubmitMessageRequest {
  text: string
}

export interface AgentSubmitMessageResponse {
  message_id: string
  conversation_id: string
  admission_seq: number
  idempotency_key: string
  delivery_state: string
  submission_state: string
}

export interface AgentCancelRequest {
  reason?: string | null
}

export interface AgentCancelResponse {
  outcome: string
  run_state: string
}

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

    /** Admit one plain-text user message (M4.5): 202 on durable enqueue. */
    submitMessage: (conversationId: string, body: AgentSubmitMessageRequest, signal?: AbortSignal) =>
      request<AgentSubmitMessageResponse>(
        conversationPath(conversationId, '/messages'),
        withSignal({ method: 'POST', body }, signal),
      ),

    /** Cancel the conversation's active run (M4.5): 202 or 409 no_active_run. */
    cancelRun: (conversationId: string, body: AgentCancelRequest = {}, signal?: AbortSignal) =>
      request<AgentCancelResponse>(
        conversationPath(conversationId, '/cancel'),
        withSignal({ method: 'POST', body }, signal),
      ),
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

/**
 * Envelope of the agui replay pages: identical shape to
 * ``AgentEventListResponse`` but with untyped projected event objects
 * (validated per event by ``parseAguiEvent``). ``next_cursor`` keeps its
 * ``database_seq`` semantics (M6a spec §4.5).
 */
export interface AguiEventListResponse {
  events: unknown[]
  next_cursor: number | null
}

/**
 * Page the conversation's AG-UI projected events after ``sinceSeq`` (M6b
 * spec §5). Paging continues only while ``next_cursor`` advances; an empty
 * page stops the loop. Malformed or unknown event objects are discarded
 * (never thrown). Returns the validated events plus the seq watermark:
 * ``coveredThrough`` is the terminating ``next_cursor`` — for an empty
 * result it is the original ``since``, per the endpoint contract.
 */
export async function fetchEventsSinceAgui(
  request: ApiRequest,
  conversationId: string,
  sinceSeq: number,
): Promise<AguiReplayBatch> {
  const events: AguiEvent[] = []
  let cursor = sinceSeq
  for (;;) {
    const page = await request<AguiEventListResponse>(
      `/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/events?wire=agui&since=${cursor}&limit=200`,
    )
    for (const raw of page.events) {
      const event = parseAguiEvent(raw)
      if (event !== null) events.push(event)
    }
    if (page.events.length === 0) return { events, coveredThrough: page.next_cursor ?? cursor }
    if (page.next_cursor === null || page.next_cursor <= cursor) return { events, coveredThrough: page.next_cursor ?? cursor }
    cursor = page.next_cursor
  }
}
