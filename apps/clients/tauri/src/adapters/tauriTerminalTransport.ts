import { Channel, invoke } from '@tauri-apps/api/core'
import type { TerminalConnectRequest, TerminalConnection, TerminalTransport, TerminalTransportEvent } from '@termflow/client-core'
import { serverConfig } from '../serverConfig'

interface WebSocketEvent { type: 'Text' | 'Binary' | 'Close'; data: string | number[] | { code: number } }

function endpoint(request: TerminalConnectRequest): { proofUrl: string; socketUrl: string } {
  const url = new URL(`/api/v1/terms/${encodeURIComponent(request.termId)}/terminal`, `${serverConfig.current}/`)
  if (request.terminalId !== undefined && request.streamId !== undefined && request.afterSeq !== undefined) {
    url.searchParams.set('terminal_id', request.terminalId); url.searchParams.set('stream_id', request.streamId); url.searchParams.set('after_seq', String(request.afterSeq))
  }
  const proofUrl = url.toString()
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return { proofUrl, socketUrl: url.toString() }
}

export function createTauriTerminalTransport(): TerminalTransport {
  return {
    async connect(request: TerminalConnectRequest, emit: (event: TerminalTransportEvent) => void): Promise<TerminalConnection> {
      const { proofUrl, socketUrl } = endpoint(request)
      const channel = new Channel<WebSocketEvent>()
      channel.onmessage = (message) => {
        if (message.type === 'Text') emit({ type: 'text', data: message.data as string })
        else if (message.type === 'Binary') emit({ type: 'binary', data: new Uint8Array(message.data as number[]) })
        else if (message.type === 'Close') emit({ type: 'close', code: (message.data as { code: number }).code })
      }
      const id = await invoke<string>('native_terminal_connect', {
        issuer: serverConfig.current,
        proofUrl,
        socketUrl,
        onMessage: channel,
      })
      emit({ type: 'open' })
      return {
        sendText: async (data) => { await invoke('native_terminal_send', { id, data: Array.from(new TextEncoder().encode(data)), isBinary: false }) },
        sendBinary: async (data) => { await invoke('native_terminal_send', { id, data: Array.from(data), isBinary: true }) },
        close: async () => { await invoke('native_terminal_close', { id }) },      }
    },
  }
}
