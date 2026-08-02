import { invoke } from '@tauri-apps/api/core'
import WebSocket from '@tauri-apps/plugin-websocket'
import type { TerminalConnectRequest, TerminalConnection, TerminalTransport, TerminalTransportEvent } from '@termflow/client-core'
import { serverConfig } from '../serverConfig'

interface NativeHeaders { authorization: string; dpop: string }

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
      const auth = await invoke<NativeHeaders>('native_request_headers', { issuer: serverConfig.current, method: 'GET', url: proofUrl })
      const socket = await WebSocket.connect(socketUrl, { headers: { Authorization: auth.authorization, DPoP: auth.dpop } })
      const unlisten = socket.addListener((message) => {
        if (message.type === 'Text') emit({ type: 'text', data: message.data })
        else if (message.type === 'Binary') emit({ type: 'binary', data: new Uint8Array(message.data) })
        else if (message.type === 'Close') emit({ type: 'close', code: message.data?.code ?? 1000 })
      })
      emit({ type: 'open' })
      return {
        sendText: async (data) => socket.send(data),
        sendBinary: async (data) => socket.send(Array.from(data)),
        close: async () => { unlisten(); await socket.disconnect() },
      }
    },
  }
}
