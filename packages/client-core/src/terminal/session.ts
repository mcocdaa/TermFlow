import {
  parseTerminalControl,
  type TerminalAction,
  type TerminalActionResultFrame,
  type TerminalBindingSnapshotFrame,
  type TerminalReadyFrame,
} from '@termflow/client-contracts'
import type {
  TerminalConnectRequest,
  TerminalConnection,
  TerminalScheduler,
  TerminalTransport,
  TerminalTransportEvent,
} from './ports'

export type TerminalConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface TerminalSessionCallbacks {
  onStatus: (status: TerminalConnectionStatus) => void
  onReady: (control: TerminalReadyFrame) => void
  onOutput: (bytes: Uint8Array) => void
  onSize: (size: { rows: number, cols: number }) => void
  onBindings: (control: TerminalBindingSnapshotFrame) => void
  onError: (error: { code: string, message?: string }) => void
  onClosed: (reason: string) => void
  onReset: () => void
  onActionResult: (result: TerminalActionResultFrame) => void
  onAuthenticationRequired: () => void
}

export interface TerminalSessionOptions {
  transport: TerminalTransport
  scheduler: TerminalScheduler
  createId: () => string
  reconnectDelayMs?: number
}

export interface TerminalSessionLike {
  connect(): void
  sendInput(data: string | Uint8Array): void
  sendAction(action: TerminalAction, options?: { targetPaneId?: string, confirmed?: boolean }): void
  dispose(): void
}

const MAX_BINARY_FRAME = 65_536

export class TerminalSession implements TerminalSessionLike {
  private connection: TerminalConnection | null = null
  private reconnectTimer: unknown | null = null
  private connectionGeneration = 0
  private disposed = false
  private suppressReconnect = false
  private ready = false
  private reconnectAttempt = 0
  private terminalId: string | null = null
  private streamId: string | null = null
  private lastSeq = 0
  private readonly reconnectDelayMs: number

  constructor(
    private readonly termId: string,
    private readonly callbacks: TerminalSessionCallbacks,
    private readonly options: TerminalSessionOptions,
  ) {
    this.reconnectDelayMs = options.reconnectDelayMs ?? 1_000
  }

  connect(): void {
    if (this.disposed) return
    this.callbacks.onStatus(this.streamId === null ? 'connecting' : 'reconnecting')
    this.ready = false
    const generation = ++this.connectionGeneration
    const request: TerminalConnectRequest = { termId: this.termId }
    if (this.terminalId !== null && this.streamId !== null) {
      request.terminalId = this.terminalId
      request.streamId = this.streamId
      request.afterSeq = this.lastSeq
    }
    this.connection = this.options.transport.connect(request, (event) => {
      if (generation === this.connectionGeneration) this.handleEvent(event)
    })
  }

  private handleEvent(event: TerminalTransportEvent): void {
    if (event.type === 'open') return
    if (event.type === 'binary') {
      if (this.ready) {
        this.lastSeq += 1
        this.callbacks.onOutput(event.data)
      }
      return
    }
    if (event.type === 'close') {
      this.handleClose(event.code)
      return
    }
    this.handleText(event.data)
  }

  private handleText(data: string): void {
    const control = parseTerminalControl(data)
    if (control === null) return
    if (control.type !== 'terminal.ready' && (!this.ready || control.terminal_id !== this.terminalId)) return

    switch (control.type) {
      case 'terminal.ready':
        if (this.streamId !== null && this.streamId !== control.stream_id) {
          this.lastSeq = 0
          this.callbacks.onReset()
        }
        this.streamId = control.stream_id
        this.terminalId = control.terminal_id
        this.ready = true
        this.reconnectAttempt = 0
        this.callbacks.onStatus('connected')
        this.callbacks.onReady(control)
        break
      case 'terminal.size':
        this.callbacks.onSize({ rows: control.rows, cols: control.cols })
        break
      case 'terminal.binding_snapshot':
        this.callbacks.onBindings(control)
        break
      case 'terminal.error':
        this.callbacks.onError({ code: control.code, message: control.message })
        break
      case 'terminal.action_result':
        this.callbacks.onActionResult(control)
        break
      case 'terminal.closed':
        this.ready = false
        this.suppressReconnect = control.reason === 'replaced' || control.reason === 'instance_offline' || control.reason === 'client_closed'
        this.callbacks.onClosed(control.reason)
        this.callbacks.onStatus('closed')
        this.connection?.close(1000, control.reason)
        break
    }
  }

  private handleClose(code: number): void {
    this.connection = null
    this.ready = false
    if (code === 4401 || code === 4403) {
      this.suppressReconnect = true
      this.callbacks.onStatus('closed')
      if (code === 4401) this.callbacks.onAuthenticationRequired()
      else this.callbacks.onError({ code: 'origin_rejected' })
      return
    }
    if (this.disposed || this.suppressReconnect) return
    this.callbacks.onStatus('reconnecting')
    const delay = Math.min(10_000, this.reconnectDelayMs * 2 ** this.reconnectAttempt)
    this.reconnectAttempt += 1
    this.reconnectTimer = this.options.scheduler.set(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  sendInput(data: string | Uint8Array): void {
    if (this.connection === null || !this.ready) return
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
    for (let offset = 0; offset < bytes.byteLength; offset += MAX_BINARY_FRAME) {
      this.connection.sendBinary(bytes.slice(offset, offset + MAX_BINARY_FRAME))
    }
  }

  sendAction(action: TerminalAction, options: { targetPaneId?: string, confirmed?: boolean } = {}): void {
    if (this.connection === null || !this.ready) return
    this.connection.sendText(JSON.stringify({
      type: 'terminal.action',
      action_id: this.options.createId(),
      action,
      target_pane_id: options.targetPaneId,
      confirmed: options.confirmed ?? false,
    }))
  }

  dispose(): void {
    this.disposed = true
    this.connectionGeneration += 1
    if (this.reconnectTimer !== null) this.options.scheduler.clear(this.reconnectTimer)
    this.reconnectTimer = null
    const connection = this.connection
    if (connection !== null && this.ready) connection.sendText(JSON.stringify({ type: 'terminal.close', reason: 'client_closed' }))
    this.ready = false
    this.connection = null
    connection?.close(1000, 'route_leave')
  }
}
