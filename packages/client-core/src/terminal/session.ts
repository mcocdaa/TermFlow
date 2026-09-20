import {
  parseTerminalControl,
  type TerminalAction,
  type TerminalActionResultFrame,
  type TerminalBindingSnapshotFrame,
  type TerminalReadyFrame,
} from '@termflow/client-contracts'
import { calculateDecorrelatedJitterDelay } from './jitter'
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
  maxReconnectDelayMs?: number
  random?: () => number
  heartbeatIntervalMs?: number
  heartbeatTimeoutMs?: number
  /**
   * Optional authentication probe used before a reconnect attempt.
   */
  probeSession?: () => Promise<boolean>
}

export interface TerminalSessionLike {
  connect(): Promise<void>
  sendInput(data: string | Uint8Array): Promise<void>
  sendAction(action: TerminalAction, options?: { targetPaneId?: string, confirmed?: boolean }): Promise<void>
  dispose(): Promise<void>
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
  private heartbeatTimer: unknown | null = null
  private lastReceivedAt = 0
  private lastPingSentAt = 0
  private lastReconnectDelay = 0
  private readonly reconnectDelayMs: number
  private readonly maxReconnectDelayMs: number
  private readonly random: () => number
  private readonly heartbeatIntervalMs: number
  private readonly heartbeatTimeoutMs: number

  constructor(
    private readonly termId: string,
    private readonly callbacks: TerminalSessionCallbacks,
    private readonly options: TerminalSessionOptions,
  ) {
    this.reconnectDelayMs = options.reconnectDelayMs ?? 1_000
    this.maxReconnectDelayMs = options.maxReconnectDelayMs ?? 10_000
    this.random = options.random ?? Math.random
    this.heartbeatIntervalMs = options.heartbeatIntervalMs ?? 15_000
    this.heartbeatTimeoutMs = options.heartbeatTimeoutMs ?? 5_000
  }

  async connect(): Promise<void> {
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
    try {
      const connection = await this.options.transport.connect(request, (event) => {
        if (generation === this.connectionGeneration) this.handleEvent(event)
      })
      if (generation !== this.connectionGeneration || this.disposed) {
        await connection.close(1000, 'stale_connect')
        return
      }
      this.connection = connection
    } catch {
      if (generation === this.connectionGeneration) this.handleClose(1006)
    }
  }

  private handleEvent(event: TerminalTransportEvent): void {
    this.lastReceivedAt = Date.now()
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
        this.lastReconnectDelay = 0
        this.lastReceivedAt = Date.now()
        this.callbacks.onStatus('connected')
        this.callbacks.onReady(control)
        this.startHeartbeat()
        break
      case 'terminal.pong':
        this.lastPingSentAt = 0
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
        this.stopHeartbeat()
        this.ready = false
        this.suppressReconnect = control.reason === 'replaced' || control.reason === 'instance_offline' || control.reason === 'client_closed'
        this.callbacks.onClosed(control.reason)
        this.callbacks.onStatus('closed')
        if (this.connection !== null) void this.connection.close(1000, control.reason)
        break
    }
  }

  private startHeartbeat(): void {
    if (this.heartbeatIntervalMs <= 0 || this.disposed) return
    this.stopHeartbeat()
    this.heartbeatTimer = this.options.scheduler.set(() => {
      this.heartbeatTimer = null
      void this.checkHeartbeat()
    }, this.heartbeatIntervalMs)
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      this.options.scheduler.clear(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  private async checkHeartbeat(): Promise<void> {
    if (this.disposed || !this.ready || this.connection === null) return
    const now = Date.now()
    if (this.lastPingSentAt > 0 && now - this.lastReceivedAt >= this.heartbeatTimeoutMs) {
      this.handleClose(1006)
      return
    }
    this.lastPingSentAt = now
    try {
      await this.connection.sendText(JSON.stringify({ type: 'terminal.ping', timestamp: now }))
    } catch {
      this.handleClose(1006)
      return
    }
    this.startHeartbeat()
  }

  private handleClose(code: number): void {
    this.stopHeartbeat()
    this.lastPingSentAt = 0
    this.connectionGeneration += 1
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
    const delay = calculateDecorrelatedJitterDelay(
      this.reconnectDelayMs,
      this.maxReconnectDelayMs,
      this.reconnectAttempt,
      this.lastReconnectDelay,
      this.random,
    )
    this.lastReconnectDelay = delay
    this.reconnectAttempt += 1
    this.reconnectTimer = this.options.scheduler.set(() => {
      this.reconnectTimer = null
      void this.reattempt()
    }, delay)
  }

  private async reattempt(): Promise<void> {
    if (this.disposed || this.suppressReconnect) return
    const probe = this.options.probeSession
    if (probe !== undefined) {
      let authenticated = true
      try {
        authenticated = await probe()
      } catch {
        authenticated = true
      }
      if (this.disposed || this.suppressReconnect) return
      if (!authenticated) {
        this.suppressReconnect = true
        this.callbacks.onStatus('closed')
        this.callbacks.onAuthenticationRequired()
        return
      }
    }
    await this.connect()
  }

  async sendInput(data: string | Uint8Array): Promise<void> {
    if (this.connection === null || !this.ready) return
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
    for (let offset = 0; offset < bytes.byteLength; offset += MAX_BINARY_FRAME) {
      await this.connection.sendBinary(bytes.slice(offset, offset + MAX_BINARY_FRAME))
    }
  }

  async sendAction(action: TerminalAction, options: { targetPaneId?: string, confirmed?: boolean } = {}): Promise<void> {
    if (this.connection === null || !this.ready) return
    await this.connection.sendText(JSON.stringify({
      type: 'terminal.action',
      action_id: this.options.createId(),
      action,
      target_pane_id: options.targetPaneId,
      confirmed: options.confirmed ?? false,
    }))
  }

  async dispose(): Promise<void> {
    this.disposed = true
    this.stopHeartbeat()
    this.lastPingSentAt = 0
    this.connectionGeneration += 1
    if (this.reconnectTimer !== null) this.options.scheduler.clear(this.reconnectTimer)
    this.reconnectTimer = null
    const connection = this.connection
    if (connection !== null && this.ready) await connection.sendText(JSON.stringify({ type: 'terminal.close', reason: 'client_closed' }))
    this.ready = false
    this.connection = null
    if (connection !== null) await connection.close(1000, 'route_leave')
  }
}
