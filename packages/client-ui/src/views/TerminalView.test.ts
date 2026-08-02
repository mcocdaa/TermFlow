import { flushPromises, mount } from '@vue/test-utils'
import type { TerminalSessionCallbacks, TerminalSessionLike } from '@termflow/client-core'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalView from './TerminalView.vue'

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
    installation_id: 'computer-1', display_name: '设计工作站', hostname: 'studio.local', platform: 'macOS', client_version: '1.2.0', online: true,
    registered_at: '2026-07-30T00:00:00Z', last_seen_at: '2026-08-01T05:00:00Z',
    terms: [{ instance_id: 'term-1', name: '产品开发', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'vim', last_seen_at: '2026-08-01T05:00:00Z' }],
  }],
}

describe('TerminalView', () => {
  it('uses only the injected runtime for terminal, topology, rename, auth redirect, and disposal', async () => {
    Object.defineProperties(window, { innerWidth: { value: 360, configurable: true }, innerHeight: { value: 800, configurable: true } })
    let callbacks!: TerminalSessionCallbacks
    const terminal: TerminalSessionLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const createTerminal = vi.fn((_termId: string, nextCallbacks: TerminalSessionCallbacks) => { callbacks = nextCallbacks; return terminal })
    const getTopology = vi.fn().mockResolvedValue(topology)
    const getDashboard = vi.fn().mockResolvedValue(dashboard)
    const rename = vi.fn().mockResolvedValue({ ...dashboard.computers[0]!.terms[0], name: '新名字' })
    const runtime = createFakeRuntime({
      api: { terms: { topology: getTopology, rename }, dashboard: { get: getDashboard } } as unknown as ClientRuntime['api'],
      createTerminal,
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/login', component: { template: '<div />' } },
        { path: '/terms/:termId', component: TerminalView },
      ],
    })
    await router.push('/terms/term-1')
    await router.isReady()
    const wrapper = mount(TerminalView, { attachTo: document.body, global: { plugins: [router, createClientUi(runtime)] } })
    await flushPromises()

    expect(createTerminal).toHaveBeenCalledWith('term-1', expect.any(Object))
    expect(terminal.connect).toHaveBeenCalledOnce()
    expect(getTopology).toHaveBeenCalledWith('term-1', expect.any(AbortSignal))
    expect(getDashboard).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(wrapper.get('[data-computer-name]').text()).toBe('设计工作站')
    expect(wrapper.get('[data-term-name]').text()).toBe('产品开发')
    expect(wrapper.get('.terminal-frame').attributes('data-display-mode')).toBe('font-100')
    expect(terminal).not.toHaveProperty('resize')

    callbacks.onStatus('connected')
    callbacks.onBindings({ type: 'terminal.binding_snapshot', terminal_id: '11111111-1111-4111-8111-111111111111', prefix: 'C-a', prefix2: null, bindings: [] })
    await flushPromises()
    const ctrl = wrapper.find('.mobile-keybar').findAll('button')[0]!
    await ctrl.trigger('click')
    expect(ctrl.attributes('aria-pressed')).toBe('true')
    const canvas = wrapper.findComponent(TerminalCanvas)
    canvas.vm.sendAction('split_left_right', { targetPaneId: '%1' })
    expect(terminal.sendAction).toHaveBeenCalledWith('split_left_right', { targetPaneId: '%1' })

    await wrapper.get('[data-term-name]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-term-name-input]').setValue('新名字')
    await wrapper.get('[data-action="save-term-name"]').trigger('click')
    await flushPromises()
    expect(rename).toHaveBeenCalledWith('term-1', '新名字', expect.any(AbortSignal))
    expect(wrapper.get('[data-term-name]').text()).toBe('新名字')

    callbacks.onActionResult({ type: 'terminal.action_result', terminal_id: '11111111-1111-4111-8111-111111111111', action_id: '22222222-2222-4222-8222-222222222222', ok: true, error_code: null })
    await flushPromises()
    expect(getTopology).toHaveBeenCalledTimes(2)

    callbacks.onAuthenticationRequired()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-1')

    wrapper.unmount()
    expect(terminal.dispose).toHaveBeenCalledOnce()
  })
})
