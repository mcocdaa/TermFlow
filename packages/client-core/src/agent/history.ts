import type { AgentMessageResponse } from '@termflow/client-contracts'
import type { AguiEvent } from './agui'

//: CUSTOM event name used by the B projection for permission-request
//: visibility (M6a spec §4.3). Real approval state stays B REST (M5.1/5.2).
export const AGUI_PERMISSION_CUSTOM = 'termflow.permission_requested'

//: Known backend runtime states projected through STATE_DELTA
//: (termflow_protocol BackendRuntimeState). Unknown values are kept verbatim
//: so wire drift never hides data; the UI maps known values to labels.
export const BACKEND_RUNTIME_STATES = ['connecting', 'busy', 'ready', 'unavailable', 'context_lost', 'reconciling', 'closed'] as const
export type BackendRuntimeState = (typeof BACKEND_RUNTIME_STATES)[number]

export function isBackendRuntimeState(value: string): value is BackendRuntimeState {
  return (BACKEND_RUNTIME_STATES as readonly string[]).includes(value)
}

export interface AgentMessageState {
  messageId: string
  role: 'assistant' | 'user' | 'system'
  text: string
  status: 'streaming' | 'complete'
  createdAt: number
}

export interface AgentToolCallState {
  toolCallId: string
  toolName: string
  status: 'running' | 'completed' | 'failed'
  summary: string | null
  startedAt: number
  endedAt: number | null
}

export interface AgentRunState {
  runId: string
  status: 'active' | 'finished' | 'error'
  errorCode: string | null
  errorMessage: string | null
  startedAt: number
  endedAt: number | null
}

export interface AgentPermissionState {
  approvalId: string
  toolName: string | null
  evidence: string | null
  expiresAt: string | null
  state: 'pending' | 'decided' | 'expired' | 'unknown'
  decidedAt: string | null
}

export interface AgentBackendState {
  /** Raw STATE_DELTA value; known members listed in ``BackendRuntimeState``. */
  state: string | null
  epoch: number | null
}

export interface AgentUserMessageState {
  clientId: string
  text: string
  deliveryState: 'pending' | 'accepted' | 'rejected'
  error: string | null
}

export type TimelineItemType = 'message' | 'tool' | 'permission' | 'run' | 'user'

export interface TimelineItem {
  type: TimelineItemType
  refId: string
  /** Event ``timestamp`` (ms) when present, otherwise creation time. */
  at: number
}

export interface AgentHistoryState {
  messages: Map<string, AgentMessageState>
  toolCalls: Map<string, AgentToolCallState>
  runs: Map<string, AgentRunState>
  permissions: Map<string, AgentPermissionState>
  backend: AgentBackendState
  /** Order = arrival order = server seq order (live and replay preserve it). */
  timeline: TimelineItem[]
  userMessages: Map<string, AgentUserMessageState>
}

export function createAgentHistoryState(): AgentHistoryState {
  return {
    messages: new Map(),
    toolCalls: new Map(),
    runs: new Map(),
    permissions: new Map(),
    backend: { state: null, epoch: null },
    timeline: [],
    userMessages: new Map(),
  }
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const nonEmptyString = (value: unknown): value is string => typeof value === 'string' && value.length > 0

function cloneState(state: AgentHistoryState): AgentHistoryState {
  return {
    messages: new Map(state.messages),
    toolCalls: new Map(state.toolCalls),
    runs: new Map(state.runs),
    permissions: new Map(state.permissions),
    backend: { ...state.backend },
    timeline: [...state.timeline],
    userMessages: new Map(state.userMessages),
  }
}

/**
 * Derive the tool status from a TOOL_CALL_RESULT summary. The B projection
 * stores a bounded summary JSON string; parse failure degrades to plain-text
 * (status completed). ``error_code``/``error_message`` mark a failed call.
 */
function toolResultStatus(content: string): 'completed' | 'failed' {
  try {
    const parsed: unknown = JSON.parse(content)
    if (record(parsed) && (parsed.error_code !== undefined || parsed.error_message !== undefined)) {
      return 'failed'
    }
  } catch {
    // Non-JSON summary: keep the raw text, treat as completed.
  }
  return 'completed'
}

/**
 * Pure reducer: fold one AG-UI event into the conversation history state
 * (M6b spec §4.5). Returns a new state (the input is never mutated), so
 * repeated or out-of-order application stays idempotent for every rule:
 *
 * - CHUNK: unknown messageId creates a streaming assistant message; known
 *   messages append the delta; chunks after ``complete`` are ignored.
 * - END: marks ``complete``; unknown messageId is ignored (replay boundary).
 * - TOOL_CALL_START/RESULT: start creates, result updates running calls
 *   (failed on error fields); RESULT without START backfills a record;
 *   repeated RESULT on a finished call is ignored.
 * - RUN_STARTED/FINISHED/ERROR: unknown runIds are backfilled defensively.
 * - CUSTOM ``termflow.permission_requested``: upserts the pending visibility
 *   entry; real approval state comes from the approvals REST.
 * - STATE_DELTA ``/backend``: overwrites ``{state, epoch}``; other paths
 *   ignored.
 *
 * ``now`` supplies the creation-time fallback when an event has no
 * ``timestamp``; inject it for deterministic tests.
 */
export function applyAguiEvent(
  state: AgentHistoryState,
  event: AguiEvent,
  now: () => number = () => Date.now(),
): AgentHistoryState {
  const next = cloneState(state)
  const at = event.timestamp ?? now()
  switch (event.type) {
    case 'TEXT_MESSAGE_CHUNK': {
      const existing = next.messages.get(event.messageId)
      if (existing === undefined) {
        next.messages.set(event.messageId, {
          messageId: event.messageId,
          role: 'assistant',
          text: event.delta,
          status: 'streaming',
          createdAt: at,
        })
        next.timeline.push({ type: 'message', refId: event.messageId, at })
      } else if (existing.status !== 'complete') {
        next.messages.set(event.messageId, { ...existing, text: existing.text + event.delta })
      }
      // chunk on a complete message is ignored (replay/live race defence).
      break
    }
    case 'TEXT_MESSAGE_END': {
      const existing = next.messages.get(event.messageId)
      // Unknown messageId is ignored: the replay boundary may start between
      // a message's chunks and its END (M6a spec §4.3).
      if (existing !== undefined && existing.status !== 'complete') {
        next.messages.set(event.messageId, { ...existing, status: 'complete' })
      }
      break
    }
    case 'TOOL_CALL_START': {
      if (!next.toolCalls.has(event.toolCallId)) {
        next.toolCalls.set(event.toolCallId, {
          toolCallId: event.toolCallId,
          toolName: event.toolCallName,
          status: 'running',
          summary: null,
          startedAt: at,
          endedAt: null,
        })
        next.timeline.push({ type: 'tool', refId: event.toolCallId, at })
      }
      break
    }
    case 'TOOL_CALL_RESULT': {
      const existing = next.toolCalls.get(event.toolCallId)
      const status = toolResultStatus(event.content)
      if (existing === undefined) {
        // Defensive backfill: a RESULT without START (replay boundary).
        next.toolCalls.set(event.toolCallId, {
          toolCallId: event.toolCallId,
          toolName: '',
          status,
          summary: event.content,
          startedAt: at,
          endedAt: at,
        })
        next.timeline.push({ type: 'tool', refId: event.toolCallId, at })
      } else if (existing.status === 'running') {
        next.toolCalls.set(event.toolCallId, { ...existing, status, summary: event.content, endedAt: at })
      }
      // Repeated RESULT on a finished call is ignored.
      break
    }
    case 'RUN_STARTED': {
      if (!next.runs.has(event.runId)) {
        next.runs.set(event.runId, {
          runId: event.runId,
          status: 'active',
          errorCode: null,
          errorMessage: null,
          startedAt: at,
          endedAt: null,
        })
        next.timeline.push({ type: 'run', refId: event.runId, at })
      }
      break
    }
    case 'RUN_FINISHED': {
      const existing = next.runs.get(event.runId)
      if (existing === undefined) {
        next.runs.set(event.runId, {
          runId: event.runId,
          status: 'finished',
          errorCode: null,
          errorMessage: null,
          startedAt: at,
          endedAt: at,
        })
        next.timeline.push({ type: 'run', refId: event.runId, at })
      } else if (existing.status === 'active') {
        next.runs.set(event.runId, { ...existing, status: 'finished', endedAt: at })
      }
      break
    }
    case 'RUN_ERROR': {
      // The pinned AG-UI 0.1.19 RUN_ERROR carries no runId (M6a §4.3
      // fixture): target the most recently started active run — B enforces
      // one active run per conversation — and drop the event when none is
      // active (there is no id to key a defensive backfill on).
      let target: AgentRunState | undefined
      let latest = -1
      for (const run of next.runs.values()) {
        if (run.status === 'active' && run.startedAt >= latest) {
          latest = run.startedAt
          target = run
        }
      }
      if (target !== undefined) {
        next.runs.set(target.runId, {
          ...target,
          status: 'error',
          errorCode: event.code ?? null,
          errorMessage: event.message,
          endedAt: at,
        })
      }
      break
    }
    case 'CUSTOM': {
      if (event.name !== AGUI_PERMISSION_CUSTOM) break
      const value = event.value
      const approvalId = value.approval_request_id
      if (!nonEmptyString(approvalId)) break
      const existing = next.permissions.get(approvalId)
      const toolName = typeof value.tool_name === 'string' ? value.tool_name : null
      const evidence = typeof value.evidence === 'string' ? value.evidence : null
      const expiresAt = typeof value.expires_at === 'string' ? value.expires_at : null
      if (existing === undefined) {
        next.permissions.set(approvalId, {
          approvalId,
          toolName,
          evidence,
          expiresAt,
          state: 'pending',
          decidedAt: null,
        })
        next.timeline.push({ type: 'permission', refId: approvalId, at })
      } else {
        // Upsert only fields the projection carries; absent optional fields
        // keep their previous value (B re-projects identical events, so a
        // refresh never clears real data). state/decidedAt stay REST-owned.
        next.permissions.set(approvalId, {
          ...existing,
          toolName: toolName ?? existing.toolName,
          evidence: evidence ?? existing.evidence,
          expiresAt: expiresAt ?? existing.expiresAt,
        })
      }
      break
    }
    case 'STATE_DELTA': {
      for (const operation of event.delta) {
        if (operation.path !== '/backend') continue
        const value = operation.value
        if (record(value) && nonEmptyString(value.state)) {
          next.backend.state = value.state
          next.backend.epoch = Number.isInteger(value.epoch) ? (value.epoch as number) : null
        }
      }
      break
    }
  }
  return next
}

/**
 * Inject historical user and system messages from the REST ``/messages``
 * listing (M6b spec §4.5). Assistant content always comes from the event
 * replay to avoid dual sources. System rows are retained as ordinary
 * completed message records so the UI can present them in a collapsed
 * context section without inventing a prompt that the server did not send.
 * Rows with a null ``body`` (pre-migration history) degrade to an empty text
 * placeholder.
 *
 * Seeding is idempotent (existing keys are skipped), and it is temporally
 * separated from same-session echoes: the 202 receipt's ``message_id`` is an
 * inbox id, not an ``agent_messages`` row id (M6b spec §4.5).
 */
export function seedUserMessages(
  state: AgentHistoryState,
  messages: readonly AgentMessageResponse[],
  now: () => number = () => Date.now(),
): AgentHistoryState {
  const next = cloneState(state)
  for (const message of messages) {
    const parsed = Date.parse(message.created_at)
    const at = Number.isFinite(parsed) ? parsed : now()
    if (message.role === 'system') {
      if (next.messages.has(message.message_id)) continue
      next.messages.set(message.message_id, {
        messageId: message.message_id,
        role: 'system',
        text: message.body ?? '',
        status: 'complete',
        createdAt: at,
      })
      next.timeline.push({ type: 'message', refId: message.message_id, at })
    } else if (message.role === 'user') {
      if (next.userMessages.has(message.message_id)) continue
      next.userMessages.set(message.message_id, {
        clientId: message.message_id,
        text: message.body ?? '',
        deliveryState: 'accepted',
        error: null,
      })
      next.timeline.push({ type: 'user', refId: message.message_id, at })
    }
  }
  return next
}
