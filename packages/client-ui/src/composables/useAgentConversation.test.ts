import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import type {
  AgentStreamConnectRequest,
  AgentStreamTransport,
  AgentStreamTransportEvent,
  AguiEvent,
} from '@termflow/client-core'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeAgentCursorStore, createFakeRuntime } from '../test/fakeRuntime'
import { createControllableClock, type ControllableClock } from '../test/voiceTestHarness'
import { useAgentConversation } from './useAgentConversation'
import { useBottomToast, type BottomToastController } from './useBottomToast'
import { useSession, type SessionActions } from './useSession'

const CONVERSATION = 'conv-1'
const CHUNK = (delta: string): AguiEvent => ({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta })

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

class ScriptedTransport implements AgentStreamTransport<AguiEvent> {
  readonly requests: AgentStreamConnectRequest[] = []
  readonly connections: Array<{ close: ReturnType<typeof vi.fn> }> = []
  readonly emitters: Array<(event: AgentStreamTransportEvent<AguiEvent>) => void> = []
  async connect(request: AgentStreamConnectRequest, emit: (event: AgentStreamTransportEvent<AguiEvent>) => void) {
    this.requests.push(request)
    const close = vi.fn(async () => undefined)
    this.connections.push({ close })
    this.emitters.push(emit)
    return { close }
  }
  emit(event: AgentStreamTransportEvent<AguiEvent>, index = this.emitters.length - 1) {
    this.emitters[index]?.(event)
  }
}

interface ConversationHarness {
  transport: ScriptedTransport
  cursorStore: ReturnType<typeof createFakeAgentCursorStore>
  clock: ControllableClock
  router: Router
  toast(): BottomToastController
  session(): SessionActions
  conversation(): ReturnType<typeof useAgentConversation>
  unmount(): void
}

function mountConversation(overrides: {
  conversationId?: string
  cursorStore?: ReturnType<typeof createFakeAgentCursorStore>
  listMessages?: ReturnType<typeof vi.fn>
  request?: ReturnType<typeof vi.fn>
  clock?: ControllableClock
} = {}): ConversationHarness {
  const transport = new ScriptedTransport()
  const cursorStore = overrides.cursorStore ?? createFakeAgentCursorStore()
  const clock = overrides.clock ?? createControllableClock()
  const listMessages = overrides.listMessages ?? vi.fn(async () => ({ messages: [] }))
  const request = overrides.request ?? vi.fn(async () => ({ events: [], next_cursor: null }))
  const runtime = createFakeRuntime({
    clock: clock.clock,
    createAgentStream: () => transport,
    agentCursorStore: cursorStore,
    api: {
      ...createFakeRuntime().api,
      agents: {
        listMessages,
        capabilities: vi.fn(async () => ({ agent_broker_enabled: true, delegated_write_grants_enabled: false, speech_to_text_enabled: false })),
        listConversations: vi.fn(async () => ({ conversations: [] })),
        createConversation: vi.fn(async () => ({})) as never,
        getConversation: vi.fn(async () => ({})) as never,
        listEvents: vi.fn(async () => ({ events: [] })) as never,
        submitMessage: vi.fn(async () => ({})) as never,
        cancelRun: vi.fn(async () => ({})) as never,
        deleteConversation: vi.fn(async () => undefined) as never,
      },
      request,
    } as unknown as ClientRuntime['api'],
  })
  const routes = [
    { path: '/agent/:conversationId', component: { template: '<div />' } },
    { path: '/login', component: { template: '<div />' } },
    { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
  ]
  const router = createRouter({ history: createMemoryHistory(), routes })
  let toast: BottomToastController | undefined
  let session: SessionActions | undefined
  let conversation: ReturnType<typeof useAgentConversation> | undefined

  const component = defineComponent({
    setup() {
      toast = useBottomToast()
      session = useSession()
      conversation = useAgentConversation({ conversationId: overrides.conversationId ?? CONVERSATION })
      return () => h('div')
    },
  })
  const wrapper = mount(component, { global: { plugins: [router, createClientUi(runtime)] } })
  return {
    transport,
    cursorStore,
    clock,
    router,
    toast: () => {
      if (toast === undefined) throw new Error('toast not captured')
      return toast
    },
    session: () => {
      if (session === undefined) throw new Error('session not captured')
      return session
    },
    conversation: () => {
      if (conversation === undefined) throw new Error('conversation not captured')
      return conversation
    },
    unmount: () => wrapper.unmount(),
  }
}

async function mounted(overrides: Parameters<typeof mountConversation>[0] = {}) {
  const harness = mountConversation(overrides)
  await harness.router.push(`/agent/${overrides.conversationId ?? CONVERSATION}`)
  await harness.router.isReady()
  await flushPromises()
  return harness
}

function messageText(harness: ConversationHarness): string {
  return harness.conversation().history.value.messages.get('m1')?.text ?? ''
}

describe('useAgentConversation', () => {
  it('offline replay: cold start connects cursor-less and seeds history from a batch REST replay', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ events: [CHUNK('一')], next_cursor: 3 })
      .mockResolvedValueOnce({ events: [], next_cursor: null })
    const harness = await mounted({ request })

    // Cold start: no persisted cursor → the session subscribes live-only…
    expect(harness.transport.requests[0]).toEqual({ conversationId: CONVERSATION })
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    // …then seeds the batch replay from seq 0 and applies it to the reducer.
    expect(request.mock.calls.some(([path]) => path.includes('/events?wire=agui&since=0'))).toBe(true)
    expect(messageText(harness)).toBe('一')
    expect(harness.conversation().status.value).toBe('connected')

    // Live frames keep flowing after the seed.
    harness.transport.emit({ type: 'event', event: CHUNK('二'), cursor: '7-4' })
    expect(messageText(harness)).toBe('一二')
  })

  it('cursor resume: a persisted cursor hot-starts the stream with it and skips the seed', async () => {
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save(CONVERSATION, '7-5', 5)
    const request = vi.fn(async () => ({ events: [], next_cursor: null }))
    const harness = await mounted({ cursorStore, request })

    expect(harness.transport.requests[0]).toEqual({ conversationId: CONVERSATION, cursor: '7-5' })
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    // Hot recovery: no REST replay, no seed.
    expect(request).not.toHaveBeenCalled()

    harness.transport.emit({ type: 'event', event: CHUNK('热'), cursor: '7-6' })
    expect(messageText(harness)).toBe('热')
  })

  it('duplicate deltas: live frames arriving during the seed are buffered and watermark-filtered', async () => {
    // Defer the first replay page so live frames arrive while it is in flight.
    const firstPage = deferred<{ events: AguiEvent[], next_cursor: number | null }>()
    const request = vi.fn((path: string) => {
      if (path.includes('since=0')) return firstPage.promise
      return Promise.resolve({ events: [], next_cursor: null })
    })
    const harness = await mounted({ request })
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    expect(request).toHaveBeenCalled()

    // Live frames during the seed: seq 2 is inside the replay's covered
    // range (dup of replayed content), seq 4 is after it.
    harness.transport.emit({ type: 'event', event: CHUNK('重复'), cursor: '7-2' })
    harness.transport.emit({ type: 'event', event: CHUNK('新'), cursor: '7-4' })
    expect(messageText(harness)).toBe('') // both buffered, nothing delivered yet

    firstPage.resolve({ events: [CHUNK('一')], next_cursor: 3 })
    await flushPromises()

    // Watermark flush: seq 2 (≤ coveredThrough 3) dropped, seq 4 delivered
    // after the replayed chunk — no duplicates, order = seq order.
    expect(messageText(harness)).toBe('一新')
  })

  it('slow client recovery: 4410 replays the gap, reconnects with the old cursor, and never repeats content', async () => {
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save(CONVERSATION, '7-1', 1)
    const request = vi.fn()
      .mockResolvedValueOnce({ events: [CHUNK('三')], next_cursor: 4 })
      .mockResolvedValueOnce({ events: [], next_cursor: null })
    const harness = await mounted({ cursorStore, request })
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    harness.transport.emit({ type: 'event', event: CHUNK('二'), cursor: '7-2' })
    expect(messageText(harness)).toBe('二')
    // Envelope cursors persist as they flow through (M6b spec §4.4).
    expect(harness.cursorStore.load(CONVERSATION)).toEqual({ cursor: '7-2', seq: 2 })

    // Slow-consumer close: session recovers through the batch replay.
    harness.transport.emit({ type: 'close', code: 4410, reason: 'too_slow' })
    await flushPromises()
    expect(harness.toast().current.value?.text).toContain('连接恢复中')
    expect(request.mock.calls.some(([path]) => path.includes('since=2'))).toBe(true)
    expect(messageText(harness)).toBe('二三')

    // The reconnect resumes with the old opaque cursor (epoch known); the
    // server-side overlap is dropped by the advanced watermark.
    harness.clock.fireTimeouts()
    await flushPromises()
    expect(harness.transport.requests).toHaveLength(2)
    expect(harness.transport.requests[1]).toEqual({ conversationId: CONVERSATION, cursor: '7-2' })
    harness.transport.emit({ type: 'open' }, 1)
    await flushPromises()
    harness.transport.emit({ type: 'event', event: CHUNK('三'), cursor: '7-3' }, 1) // ≤ watermark → dropped
    harness.transport.emit({ type: 'event', event: CHUNK('四'), cursor: '7-5' }, 1)
    expect(messageText(harness)).toBe('二三四')
  })

  it('auth epoch closure: 4401 clears the session state, redirects to login, and never reconnects', async () => {
    const harness = await mounted()
    await harness.session().refreshSession()
    expect(harness.session().sessionState.authenticated).toBe(true)

    harness.transport.emit({ type: 'close', code: 4401, reason: 'authentication_required' })
    await flushPromises()

    expect(harness.session().sessionState.authenticated).toBe(false)
    expect(harness.router.currentRoute.value.fullPath).toBe(`/login?redirect=/agent/${CONVERSATION}`)
    // No reconnect scheduled (the session treats 4401 as terminal).
    harness.clock.fireTimeouts()
    await flushPromises()
    expect(harness.transport.requests).toHaveLength(1)
  })

  it('binding revocation: 4412 marks the conversation revoked and clears the persisted cursor', async () => {
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save(CONVERSATION, '7-5', 5)
    const harness = await mounted({ cursorStore })
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    harness.transport.emit({ type: 'close', code: 4412, reason: 'binding_revoked' })
    await flushPromises()

    expect(harness.conversation().bindingRevoked.value).toBe(true)
    expect(harness.conversation().closeReason.value).toBe('binding_revoked')
    expect(harness.cursorStore.load(CONVERSATION)).toBeNull()
    // Terminal: no reconnect.
    harness.clock.fireTimeouts()
    await flushPromises()
    expect(harness.transport.requests).toHaveLength(1)
  })

  it('conversation_not_found (initial 404 mapped by the transport) also clears the cursor', async () => {
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save(CONVERSATION, '7-5', 5)
    const harness = await mounted({ cursorStore })
    harness.transport.emit({ type: 'close', code: 4412, reason: 'conversation_not_found' })
    await flushPromises()

    expect(harness.conversation().bindingRevoked.value).toBe(true)
    expect(harness.conversation().closeReason.value).toBe('conversation_not_found')
    expect(harness.cursorStore.load(CONVERSATION)).toBeNull()
  })

  it('seeds user messages before the stream connects', async () => {
    const messages = deferred<{ messages: unknown[] }>()
    const listMessages = vi.fn(() => messages.promise)
    const harness = mountConversation({ listMessages })

    // The stream must not connect until the historical user seed finished.
    expect(harness.transport.requests).toHaveLength(0)
    messages.resolve({
      messages: [{
        message_id: 'user-1',
        conversation_id: CONVERSATION,
        run_id: null,
        role: 'user',
        kind: 'text',
        assembly_revision: 0,
        is_final: true,
        body_digest: 'd',
        body: '用户历史',
        created_at: '2026-08-12T00:00:00+00:00',
      }],
    })
    await flushPromises()
    expect(harness.transport.requests).toHaveLength(1)
    expect(harness.conversation().history.value.userMessages.get('user-1')?.text).toBe('用户历史')

    harness.transport.emit({ type: 'open' })
    await flushPromises()
    harness.transport.emit({ type: 'event', event: CHUNK('助手'), cursor: '7-1' })
    // Timeline order: the seeded user row precedes the live assistant event.
    const timeline = harness.conversation().history.value.timeline
    expect(timeline[0]).toMatchObject({ type: 'user', refId: 'user-1' })
    expect(timeline[1]).toMatchObject({ type: 'message', refId: 'm1' })
  })

  it('disposes the session and ignores late frames after unmount', async () => {
    const harness = await mounted()
    harness.transport.emit({ type: 'open' })
    await flushPromises()
    harness.transport.emit({ type: 'event', event: CHUNK('一'), cursor: '7-1' })
    expect(messageText(harness)).toBe('一')

    harness.unmount()
    await flushPromises()
    expect(harness.transport.connections[0]?.close).toHaveBeenCalledWith(1000, 'client_closed')
    const textAtUnmount = messageText(harness)
    harness.transport.emit({ type: 'event', event: CHUNK('迟到'), cursor: '7-2' })
    await flushPromises()
    expect(messageText(harness)).toBe(textAtUnmount)
  })
})
