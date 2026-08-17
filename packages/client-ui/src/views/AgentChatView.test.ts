import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { AgentConversationDetailResponse } from '@termflow/client-contracts'
import type {
  AgentStreamConnectRequest,
  AgentStreamTransport,
  AgentStreamTransportEvent,
  AguiEvent,
} from '@termflow/client-core'
import { ApiError } from '@termflow/client-core'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeAgentCursorStore, createFakeRuntime } from '../test/fakeRuntime'
import AgentChatView from './AgentChatView.vue'

const CONVERSATION = 'conv-1'

const chunk = (messageId: string, delta: string): AguiEvent => ({ type: 'TEXT_MESSAGE_CHUNK', messageId, delta })
const toolStart = (toolCallId: string, toolCallName: string): AguiEvent => ({ type: 'TOOL_CALL_START', toolCallId, toolCallName })
const toolResult = (toolCallId: string, content: string): AguiEvent => ({ type: 'TOOL_CALL_RESULT', messageId: toolCallId, toolCallId, content })
const permission = (approvalId: string): AguiEvent => ({
  type: 'CUSTOM',
  name: 'termflow.permission_requested',
  value: { approval_request_id: approvalId, tool_name: 'rm' },
})
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

function detailResponse(): AgentConversationDetailResponse {
  return {
    conversation_id: CONVERSATION,
    binding_id: 'b1',
    title: '测试会话',
    status: 'open',
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
    binding: { binding_id: 'b1', profile_id: 'p1', term_id: 'term-1', status: 'ready' },
  }
}

interface ChatHarness {
  wrapper: VueWrapper
  transport: ScriptedTransport
  router: Router
  cursorStore: ReturnType<typeof createFakeAgentCursorStore>
  listMessages: ReturnType<typeof vi.fn>
  submitMessage: ReturnType<typeof vi.fn>
  cancelRun: ReturnType<typeof vi.fn>
  getConversation: ReturnType<typeof vi.fn>
  toast(): { text: string | null }
  unmount(): void
}

async function mounted(overrides: {
  capabilities?: ReturnType<typeof vi.fn>
  listMessages?: ReturnType<typeof vi.fn>
  submitMessage?: ReturnType<typeof vi.fn>
  cancelRun?: ReturnType<typeof vi.fn>
  getConversation?: ReturnType<typeof vi.fn>
  request?: ReturnType<typeof vi.fn>
  cursorStore?: ReturnType<typeof createFakeAgentCursorStore>
} = {}): Promise<ChatHarness> {
  const capabilities = overrides.capabilities ?? vi.fn(async () => ({
    agent_broker_enabled: true,
    delegated_write_grants_enabled: false,
  }))
  const listMessages = overrides.listMessages ?? vi.fn(async () => ({ messages: [] }))
  const submitMessage = overrides.submitMessage ?? vi.fn(async () => ({
    message_id: 'inbox-1',
    conversation_id: CONVERSATION,
    admission_seq: 1,
    idempotency_key: 'k1',
    delivery_state: 'accepted',
    submission_state: 'submitted',
  }))
  const cancelRun = overrides.cancelRun ?? vi.fn(async () => ({ outcome: 'confirmed', run_state: 'cancelled' }))
  const getConversation = overrides.getConversation ?? vi.fn(async () => detailResponse())
  const request = overrides.request ?? vi.fn(async (path: unknown) => {
    const url = String(path)
    // Approval card detail: reject so the 详情不可用 fallback renders.
    if (url.startsWith('/api/v1/agent/approvals/')) throw new ApiError('server', { status: 404 })
    if (url.startsWith('/api/v1/agent/approvals')) return { approvals: [] }
    if (url.includes('/events')) return { events: [], next_cursor: null }
    return {}
  })
  const cursorStore = overrides.cursorStore ?? createFakeAgentCursorStore()
  const transport = new ScriptedTransport()
  const runtime = createFakeRuntime({
    createAgentStream: () => transport,
    agentCursorStore: cursorStore,
    api: {
      ...createFakeRuntime().api,
      agents: {
        capabilities,
        listMessages,
        submitMessage,
        cancelRun,
        getConversation,
        listBindings: vi.fn(async () => ({ bindings: [] })),
        listConversations: vi.fn(async () => ({ conversations: [] })),
        createConversation: vi.fn(async () => ({})) as never,
        deleteConversation: vi.fn(async () => undefined) as never,
        listEvents: vi.fn(async () => ({ events: [] })) as never,
      },
      request,
    } as unknown as ClientRuntime['api'],
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agent', component: { template: '<div />' } },
      { path: '/agent/:conversationId', component: AgentChatView },
      { path: '/login', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  const clientUi = createClientUi(runtime)
  // Resolve the route BEFORE mounting: the view captures the route param
  // once in setup (the conversation lifecycle keys on it).
  await router.push(`/agent/${CONVERSATION}`)
  await router.isReady()
  const wrapper = mount(AgentChatView, {
    attachTo: document.body,
    global: { plugins: [router, clientUi] },
  })
  await flushPromises()
  return {
    wrapper,
    transport,
    router,
    cursorStore,
    listMessages,
    submitMessage,
    cancelRun,
    getConversation,
    toast: () => ({ text: clientUi.toast.current.value?.text ?? null }),
    unmount: () => wrapper.unmount(),
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AgentChatView', () => {
  it('renders the live message flow with tool rows and approval cards in timeline order', async () => {
    const harness = await mounted()
    harness.transport.emit({ type: 'open' })
    await flushPromises()

    harness.transport.emit({ type: 'event', event: chunk('m1', '助手一'), cursor: '7-1' })
    harness.transport.emit({ type: 'event', event: toolStart('t1', 'ls'), cursor: '7-2' })
    harness.transport.emit({ type: 'event', event: toolResult('t1', '{"ok":true}'), cursor: '7-3' })
    harness.transport.emit({ type: 'event', event: permission('p1'), cursor: '7-4' })
    harness.transport.emit({ type: 'event', event: chunk('m2', '助手二'), cursor: '7-5' })
    await flushPromises()

    const rendered = [...harness.wrapper.get('[data-agent-message-list]').element.children]
      .map((el) => (el.className as string).split(' ')[0])
    expect(rendered).toEqual(['agent-message', 'agent-tool-activity', 'agent-approval-card', 'agent-message'])
    expect(harness.wrapper.get('[data-agent-tool-status-label]').text()).toBe('已完成')
    expect(harness.wrapper.get('[data-agent-approval-card]').attributes('data-agent-approval-id')).toBe('p1')
    harness.unmount()
  })

  it('submits composer text through the ordinary message path', async () => {
    const harness = await mounted()

    await harness.wrapper.get('[data-agent-composer-input]').setValue('你好 Agent')
    await harness.wrapper.get('[data-action="send-message"]').trigger('click')
    await flushPromises()

    expect(harness.submitMessage).toHaveBeenCalledWith(CONVERSATION, { text: '你好 Agent' })
    const [, payload] = harness.submitMessage.mock.calls[0] as [string, Record<string, unknown>]
    expect(Object.keys(payload)).toEqual(['text'])
    expect(harness.wrapper.findAll('.agent-message--user')).toHaveLength(1)
    expect(harness.wrapper.get('.agent-message--user').text()).toContain('你好 Agent')
    expect((harness.wrapper.get('[data-agent-composer-input]').element as HTMLTextAreaElement).value).toBe('')
    harness.unmount()
  })
})
