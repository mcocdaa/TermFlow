import { flushPromises, mount } from '@vue/test-utils'
import {
  ApiError,
  TerminalSession,
  type AgentStreamConnectRequest,
  type AgentStreamTransport,
  type AgentStreamTransportEvent,
  type AguiEvent,
  type TerminalSessionCallbacks,
} from '@termflow/client-core'
import { AgentChatView, createClientUi, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import { createBrowserAgentCursorStore } from '../adapters/browserAgentCursorStore'
import { createBrowserTerminalTransport } from '../adapters/browserTerminalTransport'

describe('privacy contracts', () => {
  it('keeps terminal output out of storage, URL, console, and telemetry-shaped globals', () => {
    const outputSample = 'PRIVATE_TERMINAL_OUTPUT_728'
    const received: string[] = []
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const callbacks: TerminalSessionCallbacks = {
      onStatus: vi.fn(), onReady: vi.fn(), onOutput: (bytes) => received.push(new TextDecoder().decode(bytes)), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(), onAuthenticationRequired: vi.fn(),
    }
    const fakeSocket = { binaryType: '', readyState: 1, send: vi.fn(), close: vi.fn(), onmessage: null as ((event: MessageEvent) => void) | null, onopen: null, onclose: null, onerror: null }
    const session = new TerminalSession('term-privacy', callbacks, {
      transport: createBrowserTerminalTransport({ createWebSocket: () => fakeSocket as unknown as WebSocket }),
      scheduler: { set: () => 1, clear: () => undefined },
      createId: () => '33333333-3333-4333-8333-333333333333',
    })
    session.connect()
    fakeSocket.onmessage?.({ data: JSON.stringify({
      type: 'terminal.ready',
      terminal_id: '11111111-1111-4111-8111-111111111111',
      stream_id: '22222222-2222-4222-8222-222222222222',
      rows: 24,
      cols: 80,
    }) } as MessageEvent)
    fakeSocket.onmessage?.({ data: new TextEncoder().encode(outputSample) } as MessageEvent)
    expect(received).toEqual([outputSample])
    expect(JSON.stringify([...Array(localStorage.length)].map((_, index) => localStorage.getItem(localStorage.key(index)!)))).not.toContain(outputSample)
    expect(JSON.stringify([...Array(sessionStorage.length)].map((_, index) => sessionStorage.getItem(sessionStorage.key(index)!)))).not.toContain(outputSample)
    expect(window.location.href).not.toContain(outputSample)
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect((globalThis as Record<string, unknown>).telemetry).toBeUndefined()
    session.dispose()
  })
})

// ---------------------------------------------------------------------------
// M6b agent chat privacy additions (spec §6.2/§6.3): hostile agent content
// (messages, tool summaries, approval evidence) renders as literal text with
// no element injection and never leaks into storage, the URL, or the console;
// the cursor store persists only the opaque `{epoch}-{seq}` cursor — never
// message content.
// ---------------------------------------------------------------------------

const CONVERSATION_ID = 'conv-privacy'

const HOSTILE_SAMPLES = {
  script: '<script>alert(1)</script>',
  ansi: '\u001b[31mPRIVATE_AGENT_ANSI\u001b[0m',
  jsUrl: 'javascript:alert(document.cookie)',
  img: '<img src=x onerror=alert(1)>',
  iframe: '<iframe src="javascript:alert(1)"></iframe>',
} as const

const LEAK_PROBES = ['alert(1)', 'javascript:', '<script', '<img', '<iframe', 'PRIVATE_AGENT_ANSI']

class ScriptedAgentTransport implements AgentStreamTransport<AguiEvent> {
  readonly emitters: Array<(event: AgentStreamTransportEvent<AguiEvent>) => void> = []
  async connect(_request: AgentStreamConnectRequest, emit: (event: AgentStreamTransportEvent<AguiEvent>) => void) {
    this.emitters.push(emit)
    return { close: async () => undefined }
  }
  emit(event: AgentStreamTransportEvent<AguiEvent>) {
    this.emitters.at(-1)?.(event)
  }
}

async function mountAgentChat() {
  const transport = new ScriptedAgentTransport()
  const cursorStore = createBrowserAgentCursorStore()
  const request = vi.fn(async (path: unknown) => {
    const url = String(path)
    // Approval card detail: reject so the CUSTOM-event fallback renders.
    if (url.startsWith('/api/v1/agent/approvals/')) throw new ApiError('server', { status: 404 })
    if (url.startsWith('/api/v1/agent/approvals')) return { approvals: [] }
    if (url.includes('/events')) return { events: [], next_cursor: null }
    return {}
  })
  const runtime = {
    api: {
      sessions: {
        status: async () => ({ authenticated: true, expires_at: null }),
        login: async () => ({ authenticated: true, expires_at: null }),
        logout: async () => ({ authenticated: false }),
      },
      agents: {
        capabilities: async () => ({
          agent_broker_enabled: true,
          delegated_write_grants_enabled: false,
          speech_to_text_enabled: false,
        }),
        listMessages: async () => ({ messages: [] }),
        submitMessage: async () => ({}),
        cancelRun: async () => ({}),
        getConversation: async () => ({
          conversation_id: CONVERSATION_ID,
          binding_id: 'binding-1',
          title: '隐私契约会话',
          status: 'open',
          created_at: '2026-08-13T00:00:00+00:00',
          updated_at: '2026-08-13T00:00:00+00:00',
          binding: { binding_id: 'binding-1', profile_id: 'profile-1', term_id: 'term-1', status: 'pending' },
        }),
      },
      request,
    },
    createAgentStream: () => transport,
    agentCursorStore: cursorStore,
    clipboard: { writeText: async () => undefined },
    clock: {
      now: () => 0,
      setTimeout: () => 1,
      clearTimeout: () => undefined,
      setInterval: () => 1,
      clearInterval: () => undefined,
    },
    visibility: { isHidden: () => false, subscribe: () => () => undefined },
    capabilities: { manageSecurity: true, manageAuthorizedClients: true },
    authorizationCompletion: { navigate: () => undefined },
    canonicalServerUrl: 'https://control.example',
    platform: 'Linux x86_64',
  } as unknown as ClientRuntime
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agent', component: { template: '<div />' } },
      { path: '/agent/:conversationId', component: AgentChatView },
      { path: '/login', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  await router.push(`/agent/${CONVERSATION_ID}`)
  await router.isReady()
  const wrapper = mount(AgentChatView, {
    attachTo: document.body,
    global: { plugins: [router, createClientUi(runtime)] },
  })
  await flushPromises()
  // Seed replay (cold start) settles before live frames arrive.
  transport.emit({ type: 'open' })
  await flushPromises()
  return { wrapper, transport, cursorStore, request }
}

function storageSnapshot(storage: Storage): string {
  return JSON.stringify([...Array(storage.length)].map((_, index) => storage.getItem(storage.key(index)!)))
}

describe('agent chat privacy contracts', () => {
  it('renders hostile agent text literally: no element injection, no storage/URL/console leaks', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { wrapper, transport } = await mountAgentChat()

    // Hostile assistant message assembled from stream chunks.
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: HOSTILE_SAMPLES.script }, cursor: '7-1' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: HOSTILE_SAMPLES.ansi }, cursor: '7-2' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: HOSTILE_SAMPLES.jsUrl }, cursor: '7-3' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_END', messageId: 'm1' }, cursor: '7-4' })
    // Hostile tool summary JSON.
    transport.emit({ type: 'event', event: { type: 'TOOL_CALL_START', toolCallId: 't1', toolCallName: 'ls' }, cursor: '7-5' })
    transport.emit({ type: 'event', event: { type: 'TOOL_CALL_RESULT', messageId: 't1', toolCallId: 't1', content: `{"summary": "${HOSTILE_SAMPLES.img}"}` }, cursor: '7-6' })
    // Hostile approval evidence on the CUSTOM event.
    transport.emit({ type: 'event', event: { type: 'CUSTOM', name: 'termflow.permission_requested', value: { approval_request_id: 'p1', tool_name: 'rm', evidence: HOSTILE_SAMPLES.iframe } }, cursor: '7-7' })
    await flushPromises()

    // No element injection: interpolated text can never become nodes.
    expect(wrapper.findAll('script, img, iframe').length).toBe(0)
    // The message bubble renders the samples literally, ANSI stripped.
    const bubble = wrapper.get('.agent-message--assistant .agent-message__text')
    expect(bubble.text()).toContain(HOSTILE_SAMPLES.script)
    expect(bubble.text()).toContain(HOSTILE_SAMPLES.jsUrl)
    expect(bubble.text()).toContain('PRIVATE_AGENT_ANSI')
    expect(bubble.text()).not.toContain('\u001b[')
    // The tool summary renders the hostile JSON literally (after expand).
    await wrapper.get('[data-agent-tool-toggle]').trigger('click')
    const summary = wrapper.get('[data-agent-tool-summary]')
    expect(summary.find('img').exists()).toBe(false)
    expect(summary.text()).toContain(HOSTILE_SAMPLES.img)
    // The approval card renders the hostile evidence literally.
    const evidence = wrapper.get('[data-agent-approval-evidence]')
    expect(evidence.find('iframe').exists()).toBe(false)
    expect(evidence.text()).toContain(HOSTILE_SAMPLES.iframe)

    // Nothing leaks into storage, the URL, or the console.
    for (const probe of LEAK_PROBES) {
      expect(storageSnapshot(localStorage), `localStorage leaked ${probe}`).not.toContain(probe)
      expect(storageSnapshot(sessionStorage), `sessionStorage leaked ${probe}`).not.toContain(probe)
      expect(window.location.href, `URL leaked ${probe}`).not.toContain(probe)
    }
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('persists only the opaque cursor: message content never reaches the cursor store', async () => {
    const { wrapper, transport, cursorStore } = await mountAgentChat()
    const sentinel = 'PRIVATE_AGENT_728'

    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: sentinel }, cursor: '7-1' })
    transport.emit({ type: 'event', event: { type: 'TOOL_CALL_START', toolCallId: 't1', toolCallName: 'ls' }, cursor: '7-2' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_END', messageId: 'm1' }, cursor: '7-3' })
    await flushPromises()

    // The chat did see the content, and the cursor store tracked the stream.
    expect(wrapper.get('.agent-message--assistant').text()).toContain(sentinel)
    expect(cursorStore.load(CONVERSATION_ID)).toEqual({ cursor: '7-3', seq: 3 })

    // localStorage holds exactly one key: the opaque cursor entry.
    expect(localStorage.length).toBe(1)
    expect(localStorage.key(0)).toBe(`termflow.agent.cursor.${CONVERSATION_ID}`)
    const raw = localStorage.getItem(`termflow.agent.cursor.${CONVERSATION_ID}`)
    expect(raw).not.toBeNull()
    const parsed = JSON.parse(raw!) as Record<string, unknown>
    // The stored shape is exactly `{cursor, seq}` — no message content.
    expect(Object.keys(parsed).sort()).toEqual(['cursor', 'seq'])
    expect(parsed.cursor).toBe('7-3')
    expect(parsed.seq).toBe(3)
    expect(raw).not.toContain(sentinel)
    expect(storageSnapshot(localStorage)).not.toContain(sentinel)
    expect(storageSnapshot(sessionStorage)).not.toContain(sentinel)
    wrapper.unmount()
  })
})
