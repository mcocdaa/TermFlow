export interface TerminalConnectRequest {
  termId: string
  terminalId?: string
  streamId?: string
  afterSeq?: number
}

export type TerminalTransportEvent =
  | { type: 'open' }
  | { type: 'text', data: string }
  | { type: 'binary', data: Uint8Array }
  | { type: 'close', code: number }

export interface TerminalConnection {
  sendText(data: string): Promise<void>
  sendBinary(data: Uint8Array): Promise<void>
  close(code: number, reason: string): Promise<void>
}

export interface TerminalTransport {
  connect(request: TerminalConnectRequest, emit: (event: TerminalTransportEvent) => void): Promise<TerminalConnection>
}

export interface TerminalScheduler {
  set(callback: () => void | Promise<void>, delayMs: number): unknown
  clear(handle: unknown): void
}
