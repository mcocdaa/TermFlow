import type { TerminalActionResultControl, TerminalBindingControl, TerminalReadyControl } from './protocol'
import { parseTerminalControl } from './protocol'
import type { TerminalActionId } from '../api/types'

export type TerminalConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed'
export interface TerminalSocketCallbacks {
  onStatus: (status: TerminalConnectionStatus) => void
  onReady: (control: TerminalReadyControl) => void
  onOutput: (bytes: Uint8Array) => void
  onSize: (size: { rows: number; cols: number }) => void
  onBindings: (control: TerminalBindingControl) => void
  onError: (error: { code: string; message?: string }) => void
  onClosed: (reason: string) => void
  onReset: () => void
  onActionResult: (result: TerminalActionResultControl) => void
}
export interface TerminalSocketLike {
  connect(): void
  sendInput(data: string | Uint8Array): void
  sendAction(actionId: TerminalActionId, options?: { targetPaneId?: string; confirmed?: boolean }): void
  dispose(): void
}
interface TerminalSocketOptions {
  baseUrl?: URL
  createWebSocket?: (url: string) => WebSocket
  reconnectDelayMs?: number
}

const MAX_BINARY_FRAME = 65_536

export class TerminalSocket implements TerminalSocketLike {
  private socket: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private disposed = false
  private suppressReconnect = false
  private streamId: string | null = null
  private readonly baseUrl: URL
  private readonly createWebSocket: (url: string) => WebSocket
  private readonly reconnectDelayMs: number

  constructor(private readonly termId: string, private readonly callbacks: TerminalSocketCallbacks, options: TerminalSocketOptions = {}) {
    this.baseUrl = options.baseUrl ?? new URL(window.location.href)
    this.createWebSocket = options.createWebSocket ?? ((url) => new WebSocket(url))
    this.reconnectDelayMs = options.reconnectDelayMs ?? 1_000
  }

  connect() {
    if (this.disposed) return
    this.callbacks.onStatus(this.streamId ? 'reconnecting' : 'connecting')
    const url = new URL(`/api/v1/terms/${encodeURIComponent(this.termId)}/terminal`, this.baseUrl)
    url.protocol = this.baseUrl.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = this.createWebSocket(url.toString())
    this.socket = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => this.callbacks.onStatus('connected')
    socket.onmessage = (event) => this.handleMessage(event.data)
    socket.onerror = () => { /* close drives the single reconnect path */ }
    socket.onclose = () => this.handleClose()
  }

  private handleMessage(data: unknown) {
    if (data instanceof ArrayBuffer) { this.callbacks.onOutput(new Uint8Array(data)); return }
    if (ArrayBuffer.isView(data)) { this.callbacks.onOutput(new Uint8Array(data.buffer, data.byteOffset, data.byteLength)); return }
    if (typeof data !== 'string') return
    const control = parseTerminalControl(data)
    if (!control) return
    switch (control.type) {
      case 'terminal.ready':
        if (this.streamId !== null && this.streamId !== control.stream_id) this.callbacks.onReset()
        this.streamId = control.stream_id
        this.callbacks.onReady(control)
        break
      case 'terminal.size': this.callbacks.onSize({ rows: control.rows, cols: control.cols }); break
      case 'terminal.binding_snapshot': this.callbacks.onBindings(control); break
      case 'terminal.error': this.callbacks.onError({ code: control.code, message: control.message }); break
      case 'terminal.action_result': this.callbacks.onActionResult(control); break
      case 'terminal.closed':
        this.suppressReconnect = control.reason === 'replaced' || control.reason === 'instance_offline' || control.reason === 'client_closed'
        this.callbacks.onClosed(control.reason)
        this.callbacks.onStatus('closed')
        break
    }
  }

  private handleClose() {
    this.socket = null
    if (this.disposed || this.suppressReconnect) return
    this.callbacks.onStatus('reconnecting')
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect() }, this.reconnectDelayMs)
  }

  sendInput(data: string | Uint8Array) {
    if (!this.socket || this.socket.readyState !== 1) return
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
    for (let offset = 0; offset < bytes.byteLength; offset += MAX_BINARY_FRAME) this.socket.send(bytes.slice(offset, offset + MAX_BINARY_FRAME))
  }

  sendAction(action: TerminalActionId, options: { targetPaneId?: string; confirmed?: boolean } = {}) {
    if (!this.socket || this.socket.readyState !== 1) return
    this.socket.send(JSON.stringify({ type: 'terminal.action', action_id: crypto.randomUUID(), action, target_pane_id: options.targetPaneId, confirmed: options.confirmed ?? false }))
  }

  dispose() {
    this.disposed = true
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    const socket = this.socket
    this.socket = null
    socket?.close(1000, 'route_leave')
  }
}

export const createTerminalSocket = (termId: string, callbacks: TerminalSocketCallbacks): TerminalSocketLike => new TerminalSocket(termId, callbacks)
