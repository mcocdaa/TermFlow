import { describe, expect, it, vi } from 'vitest'
import type { AgentStreamConnectRequest, AgentStreamTransportEvent, AguiEvent } from '@termflow/client-core'
import { createBrowserAgentStreamTransport } from './browserAgentStreamTransport'

type FetchMock = ReturnType<typeof vi.fn>

/** Full SSE envelope for an ``agent_event`` frame (B wire shape). */
function agentEventFrame(inner: Record<string, unknown>, cursor = '7-1'): string {
  return `event: agent_event\ndata: ${JSON.stringify({ type: 'event', event: inner, cursor })}\n\n`
}

function streamingResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(stream, { status, headers: { 'content-type': 'text/event-stream' } })
}

function harness(fetchMock: FetchMock, request: AgentStreamConnectRequest = {}) {
  const events: AgentStreamTransportEvent<AguiEvent>[] = []
  const transport = createBrowserAgentStreamTransport({ wire: 'agui' }, fetchMock)
  const pending = transport.connect(request, (event) => {
    events.push(event)
  })
  return { events, pending }
}

/** Wait until the pump has consumed the stream and the frame list settles. */
async function settle(events: AgentStreamTransportEvent<AguiEvent>[]): Promise<void> {
  await vi.waitFor(() => {
    expect(events.some((event) => event.type === 'close')).toBe(true)
  })
}

describe('createBrowserAgentStreamTransport', () => {
  it('subscribes with the relative agui URL, same-origin cookies, event-stream accept, and an abort signal', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamingResponse([]))
    const { events, pending } = harness(fetchMock, { conversationId: 'conv-1', cursor: '7-3' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/agent/stream?wire=agui&conversation_id=conv-1&cursor=7-3',
      expect.objectContaining({
        credentials: 'same-origin',
        headers: { accept: 'text/event-stream' },
        signal: expect.any(AbortSignal),
      }),
    )
    await pending
    // 200 opens immediately; the already-finished empty stream closes 1006.
    expect(events).toEqual([
      { type: 'open' },
      { type: 'close', code: 1006, reason: 'transport_error' },
    ])
  })

  it('emits open after a 2xx and delivers parsed agent_event frames with their cursor', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamingResponse([
      agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'x' }, '7-1'),
      agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'y' }, '7-2'),
    ]))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events).toEqual([
      { type: 'open' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'x' }, cursor: '7-1' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'y' }, cursor: '7-2' },
      { type: 'close', code: 1006, reason: 'transport_error' },
    ])
  })
})
