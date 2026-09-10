import { TerminalSession, type TerminalSessionCallbacks } from '@termflow/client-core'
import { describe, expect, it, vi } from 'vitest'
import { createBrowserTerminalTransport } from '../adapters/browserTerminalTransport'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { App, createClientUi, useSensitiveAuthorization, useTerminalSession } from '@termflow/client-ui'
import { createFakeRuntime } from './fakeRuntime'
import { createAppRouter } from '../router'
import { createBrowserAgentCursorStore } from '../adapters/browserAgentCursorStore'

describe('privacy contracts', () => {
  it('keeps agent drafts messages credentials and disclosure out of URL storage and console', async () => {
    const markers = ['PRIVATE_DRAFT', 'PRIVATE_MESSAGE', 'PRIVATE_ROOT_TOKEN', 'PRIVATE_PROVIDER_CREDENTIAL', 'PRIVATE_DISCLOSURE']
    const logs = ['log', 'error', 'warn', 'info', 'debug'].map((method) => vi.spyOn(console, method as 'log').mockImplementation(() => undefined))
    const writes = vi.spyOn(Storage.prototype, 'setItem')
    const scroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const runtime = createFakeRuntime({ agentCursorStore: createBrowserAgentCursorStore() })
    runtime.api.agents.capabilities = async () => ({ agent_broker_enabled: true, state: 'ready', reason_code: null, delegated_write_grants_enabled: false })
    runtime.api.agents.getSetup = async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: { retention_terms: markers[4] }, private_credential: markers[3] }) as never
    const id = '11111111-1111-4111-8111-111111111111'
    runtime.api.agents.listConversations = async () => ({ conversations: [{ conversation_id: id, binding_id: 'b1', title: 'Chat' }] }) as never
    runtime.api.agents.getConversation = async () => ({ conversation_id: id, title: 'Chat', binding: { binding_id: 'b1', term_id: 't1' } }) as never
    runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
    runtime.api.agents.submitMessage = vi.fn(async () => ({ message_id: 'm1' })) as never
    runtime.api.request = async () => ({ approvals: [], events: [], next_cursor: null }) as never
    runtime.api.sessions.login = vi.fn(async () => ({ authenticated: true, expires_at: '2030-01-01' }))
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
    await router.push(`/terms/t1?agent=${id}`)
    let auth!: ReturnType<typeof useSensitiveAuthorization>
    const Root = defineComponent({ setup() { auth = useSensitiveAuthorization(); return () => h(App) } })
    const Canvas = defineComponent({ props: ['termId'], setup(props) { useTerminalSession(props.termId, ref(null)); return { resetViewport() {}, captureViewport() {}, restoreViewport() {} } }, template: '<div />' })
    const w = mount(Root, { attachTo: document.body, global: { plugins: [router, createClientUi(runtime)], stubs: { TerminalCanvas: Canvas } } })
    try {
      await flushPromises()
      await w.get('[data-agent-composer-input]').setValue(markers[0])
      expect(router.currentRoute.value.query).toEqual({ agent: id })
      await w.get('[data-agent-composer-input]').setValue(markers[1])
      await w.get('[data-action="send-message"]').trigger('click'); await flushPromises()
      expect(runtime.api.agents.submitMessage).toHaveBeenCalledWith(id, { text: markers[1] })
      const pending = auth.authorize(); await flushPromises()
      await w.get('[name="root_credential"]').setValue(markers[2])
      await w.get('[role="dialog"] form').trigger('submit'); await flushPromises()
      expect(await pending).toBe(true)
      expect(runtime.api.sessions.login).toHaveBeenCalledWith(markers[2], expect.any(AbortSignal))
      const persisted = JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, writes: writes.mock.calls, logs: logs.flatMap((spy) => spy.mock.calls), url: router.currentRoute.value.fullPath, browser: window.location.href })
      for (const marker of markers) expect(persisted).not.toContain(marker)
    } finally {
      w.unmount(); scroll.mockRestore(); writes.mockRestore(); logs.forEach((spy) => spy.mockRestore())
    }
  })
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
