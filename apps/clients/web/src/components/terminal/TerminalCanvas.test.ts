import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import TerminalCanvas from './TerminalCanvas.vue'
import type { TerminalSocketCallbacks, TerminalSocketLike } from '../../terminal/socket'
import type { TerminalAdapter, TerminalAdapterFactory } from '../../terminal/terminalAdapter'
import { selectTheme } from '../../stores/theme'

describe('TerminalCanvas', () => {
  it('creates xterm only from terminal.ready, applies only server sizes, streams bytes, and disposes everything', async () => {
    let callbacks!: TerminalSocketCallbacks
    let input!: (value: string | Uint8Array) => void
    const socket: TerminalSocketLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const adapter: TerminalAdapter = { write: vi.fn(), resize: vi.fn(), reset: vi.fn(), focus: vi.fn(), refreshTheme: vi.fn(), setInputEnabled: vi.fn(), measureCell: vi.fn(() => ({ width: 10, height: 20 })), canClientPan: vi.fn(() => false), dispose: vi.fn() }
    const createSocket = vi.fn((_id: string, nextCallbacks: TerminalSocketCallbacks) => { callbacks = nextCallbacks; return socket })
    const createAdapter: TerminalAdapterFactory = vi.fn((_host, _size, onInput) => { input = onInput; return adapter })
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9', createSocket, createAdapter } })

    expect(socket.connect).toHaveBeenCalled()
    expect(createAdapter).not.toHaveBeenCalled()
    wrapper.vm.focusPane({ pane_id: '%1', window_id: '@1', index: 0, title: 'shell', current_command: 'zsh', active: true, dead: false, left: 0, top: 0, width: 40, height: 20 })
    callbacks.onReady({ type: 'terminal.ready', terminal_id: 't1', stream_id: 's1', rows: 44, cols: 150 })
    await flushPromises()
    expect(createAdapter).toHaveBeenCalledWith(expect.any(HTMLElement), { rows: 44, cols: 150 }, expect.any(Function))
    expect(adapter.setInputEnabled).toHaveBeenCalledWith(true)
    expect(wrapper.get('.terminal-frame').attributes('data-cell-width')).toBe('10')
    expect(wrapper.get('.terminal-frame').attributes('data-cell-height')).toBe('20')
    expect(wrapper.get('.terminal-frame').attributes('data-focused-pane')).toBe('%1')
    callbacks.onOutput(new Uint8Array([1, 2]))
    callbacks.onSize({ rows: 50, cols: 170 })
    callbacks.onActionResult({ type: 'terminal.action_result', terminal_id: 't1', action_id: 'a1', ok: true })
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('action-result')).toEqual([[expect.objectContaining({ action_id: 'a1', ok: true })]])
    input('ls\r')
    await wrapper.get('.terminal-frame').trigger('pointerdown', { pointerId: 1, pointerType: 'touch', clientX: 200, clientY: 200 })
    await wrapper.get('.terminal-frame').trigger('pointermove', { pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200 })
    expect(adapter.write).toHaveBeenCalledWith(new Uint8Array([1, 2]))
    expect(adapter.resize).toHaveBeenCalledWith(170, 50)
    expect(socket.sendInput).toHaveBeenCalledWith('ls\r')
    expect(adapter.canClientPan).toHaveBeenCalled()
    callbacks.onStatus('reconnecting')
    expect(adapter.setInputEnabled).toHaveBeenLastCalledWith(false)
    expect(typeof wrapper.vm.captureViewport).toBe('function')
    expect(typeof wrapper.vm.restoreViewport).toBe('function')
    wrapper.vm.restoreViewport({ scale: 1.5, panX: -20, panY: -10, focusedPaneId: '%1' })
    expect(wrapper.vm.captureViewport().focusedPaneId).toBe('%1')
    selectTheme('cloud-cobalt')
    await wrapper.vm.$nextTick()
    expect(adapter.refreshTheme).toHaveBeenCalled()

    wrapper.unmount()
    expect(adapter.dispose).toHaveBeenCalled()
    expect(socket.dispose).toHaveBeenCalled()
  })

  it('renders stable localized errors without exposing server messages or error codes', async () => {
    let callbacks!: TerminalSocketCallbacks
    const socket: TerminalSocketLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const createSocket = vi.fn((_id: string, nextCallbacks: TerminalSocketCallbacks) => { callbacks = nextCallbacks; return socket })
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9', createSocket } })

    callbacks.onError({ code: 'instance_offline', message: 'raw backend stack trace' })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('Term 当前离线，无法打开终端。')
    expect(wrapper.text()).not.toContain('raw backend stack trace')

    callbacks.onActionResult({ type: 'terminal.action_result', terminal_id: 't1', action_id: 'a1', ok: false, error_code: 'target_not_found' })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('目标 Pane 已不存在，请刷新状态。')
    expect(wrapper.text()).not.toContain('target_not_found')

    callbacks.onError({ code: 'unknown_internal', message: 'database password leaked' })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('终端发生错误，请稍后重试。')
    expect(wrapper.text()).not.toContain('unknown_internal')
  })
})
