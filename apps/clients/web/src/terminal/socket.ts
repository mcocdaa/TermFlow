import {
  TerminalSession,
  type TerminalConnectionStatus,
  type TerminalScheduler,
  type TerminalSessionCallbacks,
  type TerminalSessionLike,
} from '@termflow/client-core'
import { createBrowserTerminalTransport } from '../adapters/browserTerminalTransport'

export type { TerminalConnectionStatus }
export type TerminalSocketCallbacks = TerminalSessionCallbacks
export type TerminalSocketLike = TerminalSessionLike

interface TerminalSocketOptions {
  baseUrl?: URL
  createWebSocket?: (url: string) => WebSocket
  reconnectDelayMs?: number
}

const browserScheduler: TerminalScheduler = {
  set: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clear: (handle) => globalThis.clearTimeout(handle as number),
}

export class TerminalSocket implements TerminalSocketLike {
  private readonly session: TerminalSession

  constructor(termId: string, callbacks: TerminalSocketCallbacks, options: TerminalSocketOptions = {}) {
    this.session = new TerminalSession(termId, callbacks, {
      transport: createBrowserTerminalTransport({
        ...(options.baseUrl === undefined ? {} : { baseUrl: options.baseUrl }),
        ...(options.createWebSocket === undefined ? {} : { createWebSocket: options.createWebSocket }),
      }),
      scheduler: browserScheduler,
      createId: () => globalThis.crypto.randomUUID(),
      ...(options.reconnectDelayMs === undefined ? {} : { reconnectDelayMs: options.reconnectDelayMs }),
    })
  }

  connect(): void { this.session.connect() }
  sendInput(data: string | Uint8Array): void { this.session.sendInput(data) }
  sendAction(action: Parameters<TerminalSession['sendAction']>[0], options?: Parameters<TerminalSession['sendAction']>[1]): void {
    this.session.sendAction(action, options)
  }
  dispose(): void { this.session.dispose() }
}

export const createTerminalSocket = (termId: string, callbacks: TerminalSocketCallbacks): TerminalSocketLike => new TerminalSocket(termId, callbacks)
