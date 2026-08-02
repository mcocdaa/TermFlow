import { flushPromises, mount } from '@vue/test-utils'
import type { TerminalSessionCallbacks, TerminalSessionLike } from '@termflow/client-core'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import type { TerminalAdapter, TerminalAdapterFactory } from '../../terminal/xtermAdapter'
import { createThemeState } from '../../theme/theme'
import TerminalCanvas from './TerminalCanvas.vue'

function terminalRuntime() {
  let callbacks!: TerminalSessionCallbacks
  const terminal: TerminalSessionLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
  const createTerminal = vi.fn((_id: string, nextCallbacks: TerminalSessionCallbacks) => { callbacks = nextCallbacks; return terminal })
  return { callbacks: () => callbacks, terminal, createTerminal, runtime: createFakeRuntime({ createTerminal }) }
}

describe('TerminalCanvas', () => {
  it('creates xterm only from terminal.ready, applies only server sizes, streams bytes, and disposes everything', async () => {
    const session = terminalRuntime()
    let input!: (value: string | Uint8Array) => void
    const adapter: TerminalAdapter = { write: vi.fn(), resize: vi.fn(), reset: vi.fn(), focus: vi.fn(), refreshTheme: vi.fn(), setInputEnabled: vi.fn(), measureCell: vi.fn(() => ({ width: 10, height: 20 })), canClientPan: vi.fn(() => false), dispose: vi.fn() }
    const createAdapter: TerminalAdapterFactory = vi.fn((_host, _size, onInput) => { input = onInput; return adapter })
    const theme = createThemeState({ load: () => null, save: vi.fn() }, { apply: vi.fn() })
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9', createAdapter }, global: { plugins: [createClientUi(session.runtime, { theme })] } })

    expect(session.createTerminal).toHaveBeenCalledWith('term-9', expect.any(Object))
    expect(session.terminal.connect).toHaveBeenCalledOnce()
    expect(createAdapter).not.toHaveBeenCalled()
    wrapper.vm.focusPane({ pane_id: '%1', window_id: '@1', index: 0, title: 'shell', current_command: 'zsh', active: true, dead: false, left: 0, top: 0, width: 40, height: 20 })
    session.callbacks().onReady({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', stream_id: '22222222-2222-4222-8222-222222222222', rows: 44, cols: 150 })
    await flushPromises()
    expect(createAdapter).toHaveBeenCalledWith(expect.any(HTMLElement), { rows: 44, cols: 150 }, expect.any(Function))
    expect(adapter.setInputEnabled).toHaveBeenCalledWith(true)
    session.callbacks().onOutput(new Uint8Array([1, 2]))
    session.callbacks().onSize({ rows: 50, cols: 170 })
    session.callbacks().onActionResult({ type: 'terminal.action_result', terminal_id: '11111111-1111-4111-8111-111111111111', action_id: '33333333-3333-4333-8333-333333333333', ok: true, error_code: null })
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('action-result')).toEqual([[expect.objectContaining({ ok: true })]])
    input('ls\r')
    expect(adapter.write).toHaveBeenCalledWith(new Uint8Array([1, 2]))
    expect(adapter.resize).toHaveBeenCalledWith(170, 50)
    expect(session.terminal.sendInput).toHaveBeenCalledWith('ls\r')
    session.callbacks().onStatus('reconnecting')
    expect(adapter.setInputEnabled).toHaveBeenLastCalledWith(false)
    theme.select('cloud-cobalt')
    await wrapper.vm.$nextTick()
    expect(adapter.refreshTheme).toHaveBeenCalled()

    wrapper.unmount()
    expect(adapter.dispose).toHaveBeenCalled()
    expect(session.terminal.dispose).toHaveBeenCalled()
  })

  it('renders stable localized errors without exposing server messages or error codes', async () => {
    const session = terminalRuntime()
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9' }, global: { plugins: [createClientUi(session.runtime)] } })

    session.callbacks().onError({ code: 'instance_offline', message: 'raw backend stack trace' })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('Term 当前离线，无法打开终端。')
    expect(wrapper.text()).not.toContain('raw backend stack trace')

    session.callbacks().onActionResult({ type: 'terminal.action_result', terminal_id: '11111111-1111-4111-8111-111111111111', action_id: '33333333-3333-4333-8333-333333333333', ok: false, error_code: 'target_not_found' })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('目标 Pane 已不存在，请刷新状态。')
    expect(wrapper.text()).not.toContain('target_not_found')

    session.callbacks().onClosed('stream_gap')
    session.callbacks().onStatus('reconnecting')
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('终端连接已关闭。')
    session.callbacks().onStatus('connected')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })
})
