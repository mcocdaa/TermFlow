//: Conversation-scoped Agent chat lifecycle (M6b spec §4.4/§4.7): restores
//: the persisted cursor (hot) or cold-starts with a seed, seeds historical
//: user messages before the stream connects, wires the client-core
//: AgentStreamSession to the reducer-driven reactive history, persists
//: cursors from every envelope frame, and owns the terminal outcomes —
//: 4401 → clearSessionState + /login redirect, 4412 → bindingRevoked +
//: cursor clear, 4410 → "recovering" toast while the session replays.
import {
  AgentStreamSession,
  AGENT_STREAM_CLOSE_BINDING_REVOKED,
  applyAguiEvent,
  createAgentHistoryState,
  fetchEventsSinceAgui,
  parseCursorSeq,
  seedUserMessages,
  type AgentHistoryState,
  type AgentStreamStatus,
  type AgentStreamTransport,
  type AguiEvent,
} from '@termflow/client-core'
import { onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useClientRuntime } from '../runtime'
import { useSession } from './useSession'
import { useBottomToast } from './useBottomToast'

export interface UseAgentConversationOptions {
  conversationId: string
  /**
   * Capability gate (M6b spec §6.4): while false the historical seed and
   * the stream connect never run — a disabled broker must not trigger
   * Agent API requests. Flipping it true starts the lifecycle once.
   */
  enabled?: MaybeRefOrGetter<boolean>
}

export function useAgentConversation(options: UseAgentConversationOptions) {
  const runtime = useClientRuntime()
  const toast = useBottomToast()
  const { clearSessionState } = useSession()
  const router = useRouter()
  const route = useRoute()
  const history = ref<AgentHistoryState>(createAgentHistoryState())
  const status = ref<AgentStreamStatus>('connecting')
  const bindingRevoked = ref(false)
  const closeReason = ref<string | null>(null)
  const controller = new AbortController()
  let disposed = false
  let session: AgentStreamSession<AguiEvent> | null = null

  function handleAuthenticationRequired() {
    clearSessionState()
    void router.replace({ path: '/login', query: { redirect: route.fullPath } })
  }

  /**
   * Wrap the runtime transport so every envelope cursor is persisted as it
   * flows through (event and reset frames). Persistence is an optimization —
   * the cold-start seed is the correctness fallback (M6b spec §4.4).
   */
  function persistingTransport(): AgentStreamTransport<AguiEvent> {
    const base = runtime.createAgentStream()
    return {
      connect: async (request, emit) => base.connect(request, (event) => {
        if (event.type === 'event' || event.type === 'reset') {
          const seq = parseCursorSeq(event.cursor)
          if (seq !== null) runtime.agentCursorStore.save(options.conversationId, event.cursor, seq)
        }
        emit(event)
      }),
    }
  }

  onMounted(() => {
    void start()
  })

  /** Cold/hot start sequence (M6b spec §4.4); runs once, gated by `enabled`. */
  let started = false
  async function start() {
    if (started || !toValue(options.enabled ?? true)) return
    started = true
    const stored = runtime.agentCursorStore.load(options.conversationId)
    // Seed historical user messages before the stream connects: the reducer
    // timeline is application-ordered, so user rows must precede replayed
    // and live assistant events (M6b spec §4.5 timing split). Best effort —
    // a failed seed leaves user history empty, the stream still runs.
    try {
      const messages = await runtime.api.agents.listMessages(options.conversationId, { signal: controller.signal })
      if (!disposed) history.value = seedUserMessages(history.value, messages.messages, () => runtime.clock.now())
    } catch {
      // Aborted (unmount) or offline: proceed without user history.
    }
    if (disposed) return

    session = new AgentStreamSession<AguiEvent>(options.conversationId, {
      onEvent: (event) => {
        history.value = applyAguiEvent(history.value, event, () => runtime.clock.now())
      },
      onStatus: (next) => {
        status.value = next
      },
      onReset: () => {
        // The persisted cursor is refreshed by the transport wrapper; the
        // reducer stays untouched (already-delivered events stay valid).
      },
      onClosed: (info) => {
        status.value = 'closed'
        if (info.code === AGENT_STREAM_CLOSE_BINDING_REVOKED) {
          bindingRevoked.value = true
          closeReason.value = info.reason
          // Revoked bindings and deleted conversations can never resume
          // (M6b spec §4.4 clear timing).
          runtime.agentCursorStore.clear(options.conversationId)
        }
      },
      onError: (error) => {
        if (error.code === 'stream_too_slow') {
          toast.show({ text: '连接恢复中，正在补齐消息…', tone: 'error' })
        }
      },
      onAuthenticationRequired: handleAuthenticationRequired,
    }, {
      transport: persistingTransport(),
      scheduler: {
        set: (callback, delayMs) => runtime.clock.setTimeout(callback, delayMs),
        clear: (handle) => runtime.clock.clearTimeout(handle),
      },
      replayAgui: (conversationId, sinceSeq) => fetchEventsSinceAgui(runtime.api.request, conversationId, sinceSeq),
      // Hot recovery resumes from the persisted cursor; a cold start seeds
      // from seq 0 with a batch REST replay after open (§4.4).
      ...(stored === null ? { seedFromSeq: 0 } : { initialCursor: stored.cursor }),
    })
    void session.connect()
  }

  // The capability gate usually resolves right after mount; start once it
  // opens (a disabled broker never triggers a request).
  watch(() => toValue(options.enabled ?? true), (enabled) => {
    if (enabled) void start()
  })

  onBeforeUnmount(() => {
    disposed = true
    controller.abort()
    void session?.dispose()
  })

  return { history, status, bindingRevoked, closeReason }
}
