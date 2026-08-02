import { flushPromises, mount } from '@vue/test-utils'
import type { TerminalSessionCallbacks, TerminalSessionLike } from '@termflow/client-core'
import { afterEach, describe, expect, it, vi } from 'vitest'
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

function adapterDouble(overrides: Partial<TerminalAdapter> = {}): TerminalAdapter {
  return {
    write: vi.fn(), resize: vi.fn(), reset: vi.fn(), focus: vi.fn(), refreshTheme: vi.fn(), setInputEnabled: vi.fn(),
    measureCell: vi.fn(() => ({ width: 10, height: 20 })),
    setVisualScale: vi.fn((scale: number) => ({ width: 10 * scale, height: 20 * scale })),
    canClientPan: vi.fn(() => true), canNativeWheel: vi.fn(() => true), dispatchMouse: vi.fn(), dispose: vi.fn(),
    ...overrides,
  }
}

function mountTouchCanvas(viewportLocked: boolean, overrides: Partial<TerminalAdapter> = {}) {
  const session = terminalRuntime()
  const adapter = adapterDouble(overrides)
  const createAdapter: TerminalAdapterFactory = vi.fn(() => adapter)
  const wrapper = mount(TerminalCanvas, {
    props: { termId: 'term-touch', viewportLocked, createAdapter },
    global: { plugins: [createClientUi(session.runtime)] },
  })
  const ready = () => session.callbacks().onReady({
    type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', stream_id: '22222222-2222-4222-8222-222222222222', rows: 40, cols: 120,
  })
  return { ...session, wrapper, adapter, dispatchMouse: adapter.dispatchMouse as ReturnType<typeof vi.fn>, ready }
}

afterEach(() => vi.useRealTimers())

describe('TerminalCanvas', () => {
  it('routes unlocked touch only to local pan and pinch', async () => {
    const { wrapper, dispatchMouse, terminal, ready } = mountTouchCanvas(false)
    ready()
    await flushPromises()
    const frame = wrapper.get('.terminal-frame')
    await frame.trigger('pointerdown', { pointerId: 1, pointerType: 'touch', clientX: 200, clientY: 200 })
    await frame.trigger('pointermove', { pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200 })
    await frame.trigger('pointerup', { pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200 })
    expect(dispatchMouse).not.toHaveBeenCalled()
    expect(terminal.sendInput).not.toHaveBeenCalled()
    expect(wrapper.vm.captureViewport().panX).toBeLessThan(0)
  })

  it('preserves native scroll offsets across locking and resets them only on viewport reset', async () => {
    const { wrapper, ready } = mountTouchCanvas(false)
    ready()
    await flushPromises()
    const frame = wrapper.get('.terminal-frame').element as HTMLElement
    frame.scrollLeft = 120
    frame.scrollTop = 80
    Object.defineProperty(frame, 'scrollTo', {
      configurable: true,
      value: vi.fn(({ left, top }: ScrollToOptions) => {
        if (left !== undefined) frame.scrollLeft = left
        if (top !== undefined) frame.scrollTop = top
      }),
    })

    await wrapper.setProps({ viewportLocked: true })
    expect(wrapper.get('.terminal-frame').attributes('data-viewport-lock')).toBe('locked')
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([120, 80])
    await wrapper.setProps({ viewportLocked: false })
    expect(wrapper.get('.terminal-frame').attributes('data-viewport-lock')).toBe('unlocked')
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([120, 80])

    wrapper.vm.resetViewport()
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([0, 0])
  })

  it('routes native wheel overflow only while the viewport is unlocked and xterm mouse is inactive', async () => {
    const { wrapper, ready } = mountTouchCanvas(false)
    ready()
    await flushPromises()
    const frame = wrapper.get('.terminal-frame').element as HTMLElement
    Object.defineProperties(frame, {
      clientWidth: { configurable: true, value: 300 },
      clientHeight: { configurable: true, value: 200 },
      scrollWidth: { configurable: true, value: 700 },
      scrollHeight: { configurable: true, value: 500 },
    })

    const unlockedWheel = new WheelEvent('wheel', {
      bubbles: true, cancelable: true, deltaX: 90, deltaY: 70,
    })
    frame.dispatchEvent(unlockedWheel)
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([90, 70])
    expect(unlockedWheel.defaultPrevented).toBe(true)

    await wrapper.setProps({ viewportLocked: true })
    frame.dispatchEvent(new WheelEvent('wheel', {
      bubbles: true, cancelable: true, deltaX: 50, deltaY: 40,
    }))
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([90, 70])

    await wrapper.setProps({ viewportLocked: false, displayMode: 'fit' })
    frame.dispatchEvent(new WheelEvent('wheel', {
      bubbles: true, cancelable: true, deltaX: 50, deltaY: 40,
    }))
    expect([frame.scrollLeft, frame.scrollTop]).toEqual([90, 70])

    const mouseReporting = mountTouchCanvas(false, {
      canNativeWheel: vi.fn(() => false),
    })
    mouseReporting.ready()
    await flushPromises()
    const reportingFrame = mouseReporting.wrapper.get('.terminal-frame').element as HTMLElement
    Object.defineProperties(reportingFrame, {
      clientWidth: { configurable: true, value: 300 },
      clientHeight: { configurable: true, value: 200 },
      scrollWidth: { configurable: true, value: 700 },
      scrollHeight: { configurable: true, value: 500 },
    })
    const remoteWheel = new WheelEvent('wheel', {
      bubbles: true, cancelable: true, deltaX: 50, deltaY: 40,
    })
    reportingFrame.dispatchEvent(remoteWheel)
    expect([reportingFrame.scrollLeft, reportingFrame.scrollTop]).toEqual([0, 0])
    expect(remoteWheel.defaultPrevented).toBe(false)
  })

  it('routes locked touch through xterm and preserves long-press selection', async () => {
    vi.useFakeTimers()
    const { wrapper, dispatchMouse, callbacks, ready } = mountTouchCanvas(true)
    ready()
    callbacks().onStatus('connected')
    await flushPromises()
    const frame = wrapper.get('.terminal-frame')

    await frame.trigger('pointerdown', { pointerId: 1, pointerType: 'touch', clientX: 50, clientY: 60 })
    await frame.trigger('pointerup', { pointerId: 1, pointerType: 'touch', clientX: 50, clientY: 60 })
    expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mousedown', forceSelection: false }))
    expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mouseup', buttons: 0 }))

    dispatchMouse.mockClear()
    await frame.trigger('pointerdown', { pointerId: 2, pointerType: 'touch', clientX: 80, clientY: 90 })
    vi.advanceTimersByTime(500)
    expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ detail: 2, forceSelection: true }))
    await frame.trigger('pointercancel', { pointerId: 2, pointerType: 'touch', clientX: 80, clientY: 90 })
    expect(dispatchMouse.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ type: 'mouseup' }))

    callbacks().onStatus('reconnecting')
    await flushPromises()
    dispatchMouse.mockClear()
    await frame.trigger('pointerdown', { pointerId: 3, pointerType: 'touch', clientX: 100, clientY: 120 })
    await frame.trigger('pointermove', { pointerId: 3, pointerType: 'touch', clientX: 70, clientY: 120 })
    expect(dispatchMouse).not.toHaveBeenCalled()
    expect(wrapper.vm.captureViewport().panX).toBe(0)
  })

  it('renders scaled modes through xterm geometry without scaling the mouse event surface', async () => {
    const session = terminalRuntime()
    const adapter = adapterDouble({ canClientPan: vi.fn(() => false) })
    const createAdapter: TerminalAdapterFactory = vi.fn(() => adapter)
    const wrapper = mount(TerminalCanvas, {
      props: { termId: 'term-scaled', displayMode: 'scale-50', createAdapter },
      global: { plugins: [createClientUi(session.runtime)] },
    })
    session.callbacks().onReady({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', stream_id: '22222222-2222-4222-8222-222222222222', rows: 40, cols: 120 })
    await flushPromises()
    expect(adapter.setVisualScale).toHaveBeenLastCalledWith(0.5)
    expect(wrapper.get('.terminal-grid').attributes('style')).toContain('width: 600px')
    expect(wrapper.get('.terminal-grid').attributes('style')).toContain('height: 400px')
    expect(wrapper.get('.terminal-grid').attributes('style')).not.toContain('scale(')
    expect(session.terminal.sendInput).not.toHaveBeenCalled()
    expect(adapter.resize).not.toHaveBeenCalled()
    await wrapper.setProps({ displayMode: 'scale-75' })
    await flushPromises()
    await wrapper.setProps({ displayMode: 'scale-50' })
    await flushPromises()
    expect(adapter.setVisualScale).toHaveBeenLastCalledWith(0.5)
  })

  it('keeps correcting fit mode until quantized xterm cell geometry fits', async () => {
    const width = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(900)
    const height = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(715)
    const session = terminalRuntime()
    const adapter = adapterDouble({
      setVisualScale: vi.fn((scale: number) => ({ width: 10 * scale, height: scale >= 0.70 ? 18 : 17.5 })),
      canClientPan: vi.fn(() => false),
    })
    const createAdapter: TerminalAdapterFactory = vi.fn(() => adapter)
    const wrapper = mount(TerminalCanvas, {
      props: { termId: 'term-fit-quantized', displayMode: 'fit', createAdapter },
      global: { plugins: [createClientUi(session.runtime)] },
    })
    session.callbacks().onReady({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', stream_id: '22222222-2222-4222-8222-222222222222', rows: 40, cols: 120 })
    await flushPromises()
    expect(adapter.setVisualScale).toHaveBeenCalledTimes(9)
    expect(wrapper.get('.terminal-grid').attributes('style')).toContain('height: 700px')
    expect(Number(wrapper.get('.terminal-frame').attributes('data-visual-scale'))).toBeLessThan(0.70)
    wrapper.unmount()
    width.mockRestore()
    height.mockRestore()
  })

  it('creates xterm only from terminal.ready, applies only server sizes, streams bytes, and disposes everything', async () => {
    const session = terminalRuntime()
    let input!: (value: string | Uint8Array) => void
    const adapter = adapterDouble({ canClientPan: vi.fn(() => false) })
    const createAdapter: TerminalAdapterFactory = vi.fn((_host, _size, onInput) => { input = onInput; return adapter })
    const theme = createThemeState({ load: () => null, save: vi.fn() }, { apply: vi.fn() })
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9', createAdapter }, global: { plugins: [createClientUi(session.runtime, { theme })] } })

    expect(session.createTerminal).toHaveBeenCalledWith('term-9', expect.any(Object))
    expect(session.terminal.connect).toHaveBeenCalledOnce()
    expect(createAdapter).not.toHaveBeenCalled()
    session.callbacks().onReady({ type: 'terminal.ready', terminal_id: '11111111-1111-4111-8111-111111111111', stream_id: '22222222-2222-4222-8222-222222222222', rows: 44, cols: 150 })
    await flushPromises()
    expect(createAdapter).toHaveBeenCalledWith(expect.any(HTMLElement), { rows: 44, cols: 150 }, expect.any(Function), 'Linux x86_64')
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
