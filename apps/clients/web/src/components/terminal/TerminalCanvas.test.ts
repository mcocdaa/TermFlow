import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import TerminalCanvas from './TerminalCanvas.vue'
import type { TerminalSocketCallbacks, TerminalSocketLike } from '../../terminal/socket'
import type { TerminalAdapter, TerminalAdapterFactory } from '../../terminal/terminalAdapter'

describe('TerminalCanvas', () => {
  it('creates xterm only from terminal.ready, applies only server sizes, streams bytes, and disposes everything', async () => {
    let callbacks!: TerminalSocketCallbacks
    let input!: (value: string | Uint8Array) => void
    const socket: TerminalSocketLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const adapter: TerminalAdapter = { write: vi.fn(), resize: vi.fn(), reset: vi.fn(), focus: vi.fn(), dispose: vi.fn() }
    const createSocket = vi.fn((_id: string, nextCallbacks: TerminalSocketCallbacks) => { callbacks = nextCallbacks; return socket })
    const createAdapter: TerminalAdapterFactory = vi.fn((_host, _size, onInput) => { input = onInput; return adapter })
    const wrapper = mount(TerminalCanvas, { props: { termId: 'term-9', createSocket, createAdapter } })

    expect(socket.connect).toHaveBeenCalled()
    expect(createAdapter).not.toHaveBeenCalled()
    callbacks.onReady({ type: 'terminal.ready', terminal_id: 't1', stream_id: 's1', rows: 44, cols: 150 })
    expect(createAdapter).toHaveBeenCalledWith(expect.any(HTMLElement), { rows: 44, cols: 150 }, expect.any(Function))
    callbacks.onOutput(new Uint8Array([1, 2]))
    callbacks.onSize({ rows: 50, cols: 170 })
    input('ls\r')
    expect(adapter.write).toHaveBeenCalledWith(new Uint8Array([1, 2]))
    expect(adapter.resize).toHaveBeenCalledWith(170, 50)
    expect(socket.sendInput).toHaveBeenCalledWith('ls\r')

    wrapper.unmount()
    expect(adapter.dispose).toHaveBeenCalled()
    expect(socket.dispose).toHaveBeenCalled()
  })
})
