//: fetch-stream Agent transport for the browser (M6b spec §4.2): fetch +
//: hand-rolled SSE line buffering instead of EventSource, because only fetch
//: exposes the initial HTTP status (401/403/404 terminal mapping) and lets
//: AbortController cancel the stream precisely. Reconnect/backoff stays in
//: AgentStreamSession — this transport never retries.
import {
  agentStreamCloseForStatus,
  parseAgentStreamFrameAgui,
  type AgentStreamConnectRequest,
  type AgentStreamConnection,
  type AgentStreamTransport,
  type AgentStreamTransportEvent,
  type AguiEvent,
} from '@termflow/client-core'

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

const AGENT_STREAM_PATH = '/api/v1/agent/stream'

const browserFetch: Fetch = (input, init) => globalThis.fetch(input, init)

/**
 * Build the agui stream URL. Relative path plus encoded query values; the
 * cursor is an opaque `{epoch}-{seq}` token, never a credential (M6b §6.1).
 */
function streamUrl(request: AgentStreamConnectRequest): string {
  const query = new URLSearchParams()
  query.set('wire', 'agui')
  if (request.conversationId !== undefined) query.set('conversation_id', request.conversationId)
  if (request.cursor !== undefined) query.set('cursor', request.cursor)
  return `${AGENT_STREAM_PATH}?${query.toString()}`
}

export function createBrowserAgentStreamTransport(
  options: { wire: 'agui' },
  fetchImplementation: Fetch = browserFetch,
): AgentStreamTransport<AguiEvent> {
  if (options.wire !== 'agui') throw new Error('unsupported agent stream wire')
  return {
    async connect(request, emit) {
      const controller = new AbortController()
      let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
      let finished = false
      /** Emit a terminal close at most once; explicit close() stays silent. */
      const finish = (code: number, reason: string) => {
        if (finished) return
        finished = true
        emit({ type: 'close', code, reason })
      }
      const close: AgentStreamConnection['close'] = async (_code, _reason) => {
        // Idempotent: the session may close a stale connection and dispose
        // it later; the abort makes every pending read settle without an
        // extra close frame (M6b spec §4.2).
        if (finished) return
        finished = true
        controller.abort()
        try {
          await reader?.cancel()
        } catch {
          // Cancellation of an already-failed stream is not an error.
        }
      }

      try {
        const response = await fetchImplementation(streamUrl(request), {
          credentials: 'same-origin',
          headers: { accept: 'text/event-stream' },
          signal: controller.signal,
        })
        if (!response.ok) {
          const mapped = agentStreamCloseForStatus(response.status)
          finish(mapped.code, mapped.reason)
          return { close }
        }
        // Open fires immediately after a 2xx, before the first frame read.
        emit({ type: 'open' })
        if (response.body === null) {
          finish(1006, 'transport_error')
          return { close }
        }
        reader = response.body.getReader()
        void pump(reader, emit, finish)
      } catch {
        // Fetch or setup failure (including the deliberate abort): the
        // abort case is already finished, everything else is a transient
        // transport error the session may retry.
        finish(1006, 'transport_error')
      }
      return { close }
    },
  }
}

/**
 * Read the response body with incremental UTF-8 decoding and `\n\n` frame
 * splitting (M6b spec §4.2). Residual partial lines stay buffered across
 * chunk boundaries; multi-byte characters spanning chunks are safe because
 * TextDecoder runs in stream mode. Malformed frames are silently dropped by
 * parseAgentStreamFrameAgui (never logged — untrusted content).
 */
async function pump(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  emit: (event: AgentStreamTransportEvent<AguiEvent>) => void,
  finish: (code: number, reason: string) => void,
): Promise<void> {
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      for (;;) {
        const boundary = buffer.indexOf('\n\n')
        if (boundary === -1) break
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const parsed = parseAgentStreamFrameAgui(frame)
        if (parsed === null) continue
        // Server `closed` frames route through finish so a later EOF cannot
        // emit a second close (at most one terminal frame per connection).
        if (parsed.type === 'close') finish(parsed.code, parsed.reason)
        else emit(parsed)
      }
    }
    // Stream ended cleanly: an incomplete trailing frame is dropped, and a
    // server-side EOF without a closed frame is a transport error.
    finish(1006, 'transport_error')
  } catch {
    finish(1006, 'transport_error')
  } finally {
    // Best-effort release of the underlying reader after an abort.
    try {
      await reader.cancel()
    } catch {
      // Already released.
    }
  }
}
