/**
 * Tauri Agent stream transport (M6 plan line 1017): the Rust
 * `native_agent_stream` command owns the SSE long connection — it pins the
 * target to the canonical issuer origin and the `/api/` surface, signs DPoP
 * locally, and forwards complete SSE frames over an IPC channel. The WebView
 * makes no network requests for the Agent stream, so no new WebView network
 * authority exists; the JS side only parses frames into the client-core
 * `AgentStreamTransportEvent` union. Reconnect/backoff stays in
 * `AgentStreamSession` — this transport never retries.
 *
 * Command contract (src-tauri/src/agent_stream.rs):
 *   native_agent_stream { issuer, urlParams: { conversationId?, cursor? },
 *                         channel, requestId }
 *     → resolves when the connection ends (normally, on transport error, or
 *       on cancel); rejects with a Rust `safe_error` string
 *   native_agent_stream_cancel { requestId } → idempotent no-op for
 *     finished/unknown streams, so close() can call it unconditionally
 *
 * Channel frames (serde-tagged `AgentStreamFrame`):
 *   { type: 'open' } — the 2xx handshake succeeded, before the body
 *   { type: 'event', data: <raw SSE frame> } — one complete frame; parsed
 *     here by `parseAgentStreamFrameAgui` so Rust never parses untrusted
 *     server content
 *   { type: 'close', code, reason } — terminal, from the Rust side only
 *     (initial non-2xx status, EOF, or transport error)
 *
 * The adapter owns the at-most-one-terminal-frame guard, mirroring the
 * browser transport's `finish` (M6b spec §4.2): every path ends in exactly
 * one close frame — a Rust close frame, a 1006 `transport_error` after the
 * command settles without one (EOF/cancel), or a 1006 with the sanitized
 * safe error text when the command itself rejects.
 */

import { Channel, invoke } from '@tauri-apps/api/core'
import {
  parseAgentStreamFrameAgui,
  type AgentStreamConnection,
  type AgentStreamConnectRequest,
  type AgentStreamTransport,
  type AguiEvent,
} from '@termflow/client-core'
import { logNativeEvent, sanitizeNativeDetail } from '../diagnostics'
import { serverConfig } from '../serverConfig'

/** One IPC channel payload from the Rust `native_agent_stream` command. */
interface NativeAgentStreamFrame {
  type: 'open' | 'event' | 'close'
  data?: string
  code?: number
  reason?: string
}

export function createTauriAgentStreamTransport(options: { wire: 'agui' }): AgentStreamTransport<AguiEvent> {
  if (options.wire !== 'agui') throw new Error('unsupported agent stream wire')
  return {
    async connect(request: AgentStreamConnectRequest, emit): Promise<AgentStreamConnection> {
      const requestId = globalThis.crypto.randomUUID()
      /** Emit a terminal close at most once; later frames/settlements are ignored. */
      let finished = false
      const finish = (code: number, reason: string) => {
        if (finished) return
        finished = true
        emit({ type: 'close', code, reason })
      }

      const channel = new Channel<NativeAgentStreamFrame>()
      channel.onmessage = (frame) => {
        if (finished) return
        if (frame.type === 'open') {
          emit({ type: 'open' })
          return
        }
        if (frame.type === 'close') {
          finish(frame.code ?? 1006, frame.reason ?? 'transport_error')
          return
        }
        // `event`: parse the raw SSE frame. Malformed and unknown frames are
        // silently dropped by parseAgentStreamFrameAgui — never logged,
        // because the content is untrusted (mirrors the browser pump).
        const parsed = parseAgentStreamFrameAgui(frame.data ?? '')
        if (parsed === null) return
        // A server `closed` frame routes through finish so a later command
        // settlement cannot emit a second terminal frame.
        if (parsed.type === 'close') finish(parsed.code, parsed.reason)
        else emit(parsed)
      }

      const close: AgentStreamConnection['close'] = async (_code, _reason) => {
        // Idempotent: the Rust cancel treats finished/unknown ids as a
        // no-op, so stale or repeated closes are always safe (M6b spec
        // §4.2). The command resolves after the cancel and the settle
        // handler below owns the terminal frame.
        try {
          await invoke<void>('native_agent_stream_cancel', { requestId })
        } catch (error) {
          // A failed cancel must never throw out of close() or emit a
          // spurious terminal frame — the session is already gone.
          void logNativeEvent({
            event: 'agent_stream_cancel_failed',
            issuer: serverConfig.current,
            requestId,
            level: 'error',
            errorCode: 'offline',
            errorDetail: sanitizeNativeDetail(error),
          })
        }
      }

      invoke<void>('native_agent_stream', {
        issuer: serverConfig.current,
        urlParams: {
          ...(request.conversationId === undefined ? {} : { conversationId: request.conversationId }),
          ...(request.cursor === undefined ? {} : { cursor: request.cursor }),
        },
        channel,
        requestId,
      }).then(
        () => {
          // The command resolved (stream ended or was cancelled): guarantee
          // the terminal frame the Rust side may have skipped so the session
          // never waits on a connection that already settled.
          finish(1006, 'transport_error')
        },
        (error: unknown) => {
          // The command rejected before or without a channel close frame:
          // surface a transient 1006 with the sanitized safe error text and
          // a diagnostic; reconnect semantics stay with the session.
          const detail = sanitizeNativeDetail(error)
          void logNativeEvent({
            event: 'agent_stream_failed',
            issuer: serverConfig.current,
            requestId,
            level: 'error',
            errorCode: 'offline',
            errorDetail: detail,
          })
          finish(1006, detail === '' ? 'transport_error' : detail)
        },
      )

      return { close }
    },
  }
}
