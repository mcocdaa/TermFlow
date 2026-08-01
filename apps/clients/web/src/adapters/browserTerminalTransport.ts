import type {
  TerminalConnectRequest,
  TerminalConnection,
  TerminalTransport,
  TerminalTransportEvent,
} from '@termflow/client-core'

export interface BrowserTerminalTransportOptions {
  baseUrl?: URL
  createWebSocket?: (url: string) => WebSocket
}

const currentUrl = () => new URL(globalThis.location.href)
const browserWebSocket = (url: string) => new WebSocket(url)

function endpoint(request: TerminalConnectRequest, baseUrl: URL): string {
  const url = new URL(`/api/v1/terms/${encodeURIComponent(request.termId)}/terminal`, baseUrl)
  url.protocol = baseUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  if (request.terminalId !== undefined && request.streamId !== undefined && request.afterSeq !== undefined) {
    url.searchParams.set('terminal_id', request.terminalId)
    url.searchParams.set('stream_id', request.streamId)
    url.searchParams.set('after_seq', String(request.afterSeq))
  }
  return url.toString()
}

function binary(data: ArrayBuffer | ArrayBufferView): Uint8Array {
  return data instanceof ArrayBuffer
    ? new Uint8Array(data)
    : new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
}

export function createBrowserTerminalTransport(options: BrowserTerminalTransportOptions = {}): TerminalTransport {
  return {
    connect(request: TerminalConnectRequest, emit: (event: TerminalTransportEvent) => void): TerminalConnection {
      const socket = (options.createWebSocket ?? browserWebSocket)(endpoint(request, options.baseUrl ?? currentUrl()))
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => emit({ type: 'open' })
      socket.onmessage = (event) => {
        if (typeof event.data === 'string') emit({ type: 'text', data: event.data })
        else if (event.data instanceof ArrayBuffer || ArrayBuffer.isView(event.data)) emit({ type: 'binary', data: binary(event.data) })
      }
      socket.onerror = () => { /* close owns reconnect policy */ }
      socket.onclose = (event) => emit({ type: 'close', code: event.code })
      return {
        sendText: (data) => socket.send(data),
        sendBinary: (data) => socket.send(data),
        close: (code, reason) => socket.close(code, reason),
      }
    },
  }
}
