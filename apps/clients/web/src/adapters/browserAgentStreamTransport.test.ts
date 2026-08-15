import { describe, expect, it, vi } from 'vitest'
import type { AgentStreamConnectRequest, AgentStreamTransportEvent, AguiEvent } from '@termflow/client-core'
import { createBrowserAgentStreamTransport } from './browserAgentStreamTransport'

type FetchMock = ReturnType<typeof vi.fn>

/** Full SSE envelope for an ``agent_event`` frame (B wire shape). */
function agentEventFrame(inner: Record<string, unknown>, cursor = '7-1'): string {
  return `event: agent_event\ndata: ${JSON.stringify({ type: 'event', event: inner, cursor })}\n\n`
}

const CHUNK_INNER = { type: 'TEXT_MESSAGE_CHUNK', messageId: 'message-1', delta: '你好' }

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

  it('decodes multi-byte UTF-8 split across chunks and buffers partial frames', async () => {
    // Split the envelope so the three-byte 你 straddles a chunk boundary.
    const full = agentEventFrame(CHUNK_INNER, '7-1')
    const bytes = new TextEncoder().encode(full)
    const splitAt = new TextEncoder().encode(full.slice(0, full.indexOf('你'))).length + 1
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, splitAt))
        controller.enqueue(bytes.slice(splitAt))
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events.filter((event) => event.type === 'event')).toEqual([
      { type: 'event', event: CHUNK_INNER, cursor: '7-1' },
    ])
  })

  it('splits multiple frames per chunk and completes a trailing partial frame from the next chunk', async () => {
    const third = agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '三' }, '7-3')
    const splitAt = Math.floor(third.length / 2)
    const fetchMock = vi.fn().mockResolvedValue(streamingResponse([
      agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '一' }, '7-1')
        + agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '二' }, '7-2')
        + third.slice(0, splitAt),
      third.slice(splitAt),
    ]))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events.filter((event) => event.type === 'event')).toEqual([
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '一' }, cursor: '7-1' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '二' }, cursor: '7-2' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '三' }, cursor: '7-3' },
    ])
  })

  it('forwards reset and closed frames exactly once (EOF does not double-close)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamingResponse([
      'event: reset\ndata: {"cursor":"8-0"}\n\n',
      'event: closed\ndata: {"code":4410,"reason":"too_slow"}\n\n',
    ]))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events).toEqual([
      { type: 'open' },
      { type: 'reset', cursor: '8-0' },
      { type: 'close', code: 4410, reason: 'too_slow' },
    ])
  })

  it('silently drops malformed and unknown frames without logging content', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const fetchMock = vi.fn().mockResolvedValue(streamingResponse([
      'event: agent_event\ndata: {broken json}\n\n',
      'event: agent_event\ndata: {"type":"UNKNOWN_KIND"}\n\n',
      agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'ok' }, '7-1'),
    ]))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events.filter((event) => event.type === 'event')).toEqual([
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'ok' }, cursor: '7-1' },
    ])
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })

  it.each([
    [401, 4401, 'authentication_required'],
    [403, 4412, 'binding_revoked'],
    [404, 4412, 'conversation_not_found'],
    [400, 4412, 'invalid_cursor'],
    [500, 1006, 'http_error'],
    [503, 1006, 'http_error'],
  ])('maps initial HTTP %i to a terminal close %i %s without opening', async (status, code, reason) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('denied', { status }))
    const { events, pending } = harness(fetchMock)
    const connection = await pending
    expect(events).toEqual([{ type: 'close', code, reason }])
    await connection.close(1000, 'done')
    expect(events).toHaveLength(1)
  })

  it('maps fetch failures to a transient 1006 transport_error close', async () => {
    const offline = vi.fn().mockRejectedValue(new TypeError('network down'))
    const { events, pending } = harness(offline)
    await pending
    expect(events).toEqual([{ type: 'close', code: 1006, reason: 'transport_error' }])
  })

  it('maps mid-stream read failures to 1006 after open', async () => {
    const broken = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: agent_eve'))
        controller.error(new Error('connection reset'))
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(broken, { status: 200 }))
    const { events, pending } = harness(fetchMock)
    await pending
    await settle(events)
    expect(events[0]).toEqual({ type: 'open' })
    expect(events.at(-1)).toEqual({ type: 'close', code: 1006, reason: 'transport_error' })
  })

  it('close is idempotent: it cancels a pending read without emitting a close frame', async () => {
    let cancelCalled = false
    // A never-ending stream: only close() interrupts the pending read.
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'x' }, '7-1')))
      },
      cancel() {
        cancelCalled = true
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }))
    const { events, pending } = harness(fetchMock)
    const connection = await pending
    await vi.waitFor(() => {
      expect(events.filter((event) => event.type === 'event')).toHaveLength(1)
    })
    expect(events[0]).toEqual({ type: 'open' })
    await connection.close(1000, 'client_closed')
    await connection.close(1000, 'client_closed')
    expect(cancelCalled).toBe(true)
    expect(events.filter((event) => event.type === 'close')).toHaveLength(0)
  })

  it('rejects an unsupported wire at construction', () => {
    expect(() => createBrowserAgentStreamTransport({ wire: 'canonical' as never })).toThrow('unsupported agent stream wire')
  })
})
