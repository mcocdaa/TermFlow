import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'

class QuietWebSocket {
  static readonly OPEN = 1
  static readonly instances: QuietWebSocket[] = []
  binaryType: BinaryType = 'blob'
  readyState = 0
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor() { QuietWebSocket.instances.push(this) }
}

const topology = {
  instance_id: 'term-1',
  topology: {
    session_id: '$1', session_name: '产品开发', revision: 3,
    windows: [{ window_id: '@1', index: 0, name: 'editor', active: true, panes: [
      { pane_id: '%1', window_id: '@1', index: 0, title: 'vim', current_command: 'vim', active: true, dead: false, left: 0, top: 0, width: 120, height: 40 },
    ] }],
  },
}

const dashboard = {
  metrics: { online_terms: 1, total_terms: 1, active_panes: 1, interactions_24h: 8, computers: 1 },
  computers: [{
    installation_id: 'computer-1', display_name: '设计工作站', hostname: 'studio.local', platform: 'macOS', client_version: '1.2.0', online: true, last_seen_at: '2026-08-01T05:00:00Z',
    terms: [{ instance_id: 'term-1', name: '产品开发', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'vim', last_seen_at: '2026-08-01T05:00:00Z' }],
  }],
}

describe('TerminalView', () => {
  it('uses a terminal-only shell with back, editable Term name, Computer, and connection status', async () => {
    QuietWebSocket.instances.length = 0
    Object.defineProperties(window, { innerWidth: { value: 360, configurable: true }, innerHeight: { value: 800, configurable: true } })
    vi.stubGlobal('WebSocket', QuietWebSocket)
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/instances/term-1/topology') return new Response(JSON.stringify(topology), { status: 200, headers: { 'content-type': 'application/json' } })
      if (url === '/api/v1/dashboard') return new Response(JSON.stringify(dashboard), { status: 200, headers: { 'content-type': 'application/json' } })
      if (url === '/api/v1/terms/term-1' && init?.method === 'PATCH') return new Response(JSON.stringify({ ...dashboard.computers[0].terms[0], name: '新名字' }), { status: 200, headers: { 'content-type': 'application/json' } })
      return new Response(null, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/terms/term-1')
    await router.isReady()
    const wrapper = mount(App, { attachTo: document.body, global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.find('.app-header').exists()).toBe(false)
    expect(wrapper.find('.side-nav').exists()).toBe(false)
    expect(wrapper.find('.mobile-nav').exists()).toBe(false)
    expect(wrapper.get('[data-action="back-dashboard"]').attributes('href')).toBe('/')
    expect(wrapper.get('[data-computer-name]').text()).toBe('设计工作站')
    expect(wrapper.get('[data-connection-status]').text()).toContain('正在连接')
    expect(wrapper.get('[data-term-name]').text()).toBe('产品开发')
    expect(wrapper.get('.terminal-frame').attributes('data-display-mode')).toBe('font-100')

    Object.defineProperties(window, { innerWidth: { value: 800, configurable: true }, innerHeight: { value: 360, configurable: true } })
    window.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(wrapper.get('.terminal-frame').attributes('data-display-mode')).toBe('fit')

    Object.defineProperties(window, { innerWidth: { value: 360, configurable: true }, innerHeight: { value: 800, configurable: true } })
    window.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(wrapper.get('.terminal-frame').attributes('data-display-mode')).toBe('font-100')

    await wrapper.get('[data-action="edit-term-name"]').trigger('click')
    await wrapper.get('[data-term-name-input]').setValue('新名字')
    await wrapper.get('[data-action="save-term-name"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-term-name]').text()).toBe('新名字')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/terms/term-1', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: '新名字' }) }))

    wrapper.findComponent(TerminalCanvas).vm.$emit('action-result', { type: 'terminal.action_result', terminal_id: 'terminal-1', action_id: 'action-1', ok: true })
    await flushPromises()
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/instances/term-1/topology')).toHaveLength(2)

    QuietWebSocket.instances[0]?.onclose?.({ code: 4401 } as CloseEvent)
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-1')

    wrapper.unmount()
  })
})
