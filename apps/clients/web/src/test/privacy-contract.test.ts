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

const workspaceRoot = resolve(process.cwd(), '../../..')
const clientProductionFiles = () => [
  resolve(workspaceRoot, 'apps/clients/web/src'),
  resolve(workspaceRoot, 'packages/client-contracts/src'),
  resolve(workspaceRoot, 'packages/client-core/src'),
  resolve(workspaceRoot, 'packages/client-ui/src'),
].flatMap(productionFiles)

const relativeToWorkspace = (file: string) => file.replace(`${workspaceRoot}/`, '')

describe('privacy contracts', () => {
  it('allows browser persistence only for the theme identifier', () => {
    const persistence = clientProductionFiles().filter((file) => /localStorage|sessionStorage|indexedDB/i.test(readFileSync(file, 'utf8')))
    expect(persistence.map(relativeToWorkspace)).toEqual(['apps/clients/web/src/adapters/browserThemePreferences.ts'])
  })

  it('keeps shared client packages free of browser and native runtime globals', () => {
    const packageFiles = [
      resolve(workspaceRoot, 'packages/client-contracts/src'),
      resolve(workspaceRoot, 'packages/client-core/src'),
      resolve(workspaceRoot, 'packages/client-ui/src'),
    ].flatMap(productionFiles)
    const forbidden = /window\.|navigator\.|localStorage|sessionStorage|indexedDB|\bfetch\(|\bWebSocket\b|@tauri/i
    expect(packageFiles.filter((file) => forbidden.test(readFileSync(file, 'utf8'))).map(relativeToWorkspace)).toEqual([])
  })

  it('keeps terminal output out of storage, URL, console, and telemetry-shaped globals', () => {
    const outputSample = 'PRIVATE_TERMINAL_OUTPUT_728'
    const received: string[] = []
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const callbacks: TerminalSocketCallbacks = {
      onStatus: vi.fn(), onReady: vi.fn(), onOutput: (bytes) => received.push(new TextDecoder().decode(bytes)), onSize: vi.fn(), onBindings: vi.fn(), onError: vi.fn(), onClosed: vi.fn(), onReset: vi.fn(), onActionResult: vi.fn(), onAuthenticationRequired: vi.fn(),
    }
    const fakeSocket = { binaryType: '', readyState: 1, send: vi.fn(), close: vi.fn(), onmessage: null as ((event: MessageEvent) => void) | null, onopen: null, onclose: null, onerror: null }
    const socket = new TerminalSocket('term-privacy', callbacks, { createWebSocket: () => fakeSocket as unknown as WebSocket })
    socket.connect()
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
    socket.dispose()
  })
})
