import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { TerminalSocket, type TerminalSocketCallbacks } from '../terminal/socket'

function productionFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) return entry.name === 'test' ? [] : productionFiles(path)
    return /\.(ts|vue)$/.test(entry.name) && !entry.name.endsWith('.test.ts') ? [path] : []
  })
}

describe('privacy contracts', () => {
  it('allows browser persistence only for the theme identifier', () => {
    const files = productionFiles(resolve(process.cwd(), 'src'))
    const persistence = files.filter((file) => /localStorage|sessionStorage|indexedDB/.test(readFileSync(file, 'utf8')))
    expect(persistence.map((file) => file.replace(`${process.cwd()}/`, ''))).toEqual(['src/stores/theme.ts'])
  })

  it('keeps terminal output out of storage, URL, console, and telemetry-shaped globals', () => {
    const outputSample = 'PRIVATE_TERMINAL_OUTPUT_728'
    const received: string[] = []
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const callbacks: TerminalSocketCallbacks = {
      onStatus: vi.fn(), onReady: vi.fn(), onOutput: (bytes) => received.push(new TextDecoder().decode(bytes)), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(),
    }
    const fakeSocket = { binaryType: '', readyState: 1, send: vi.fn(), close: vi.fn(), onmessage: null as ((event: MessageEvent) => void) | null, onopen: null, onclose: null, onerror: null }
    const socket = new TerminalSocket('term-privacy', callbacks, { createWebSocket: () => fakeSocket as unknown as WebSocket })
    socket.connect()
    fakeSocket.onmessage?.({ data: new TextEncoder().encode(outputSample) } as MessageEvent)
    expect(received).toEqual([outputSample])
    expect(JSON.stringify([...Array(localStorage.length)].map((_, index) => localStorage.getItem(localStorage.key(index)!)))).not.toContain(outputSample)
    expect(JSON.stringify([...Array(sessionStorage.length)].map((_, index) => sessionStorage.getItem(sessionStorage.key(index)!)))).not.toContain(outputSample)
    expect(window.location.href).not.toContain(outputSample)
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect((globalThis as Record<string, unknown>).telemetry).toBeUndefined()
    socket.dispose()
  })
})
