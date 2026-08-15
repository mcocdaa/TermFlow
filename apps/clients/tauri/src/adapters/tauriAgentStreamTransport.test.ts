import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentStreamConnectRequest, AgentStreamConnection, AgentStreamTransportEvent, AguiEvent } from '@termflow/client-core'

const { invoke, logNativeEvent } = vi.hoisted(() => ({
  invoke: vi.fn(),
  logNativeEvent: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke,
  Channel: class Channel<T> { onmessage: ((message: T) => void) | undefined },
}))

vi.mock('../diagnostics', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../diagnostics')>()
  return { ...actual, logNativeEvent }
})

import { serverConfig } from '../serverConfig'
import { createTauriAgentStreamTransport } from './tauriAgentStreamTransport'

/** Full SSE envelope for an ``agent_event`` frame (B wire shape). */
function agentEventFrame(inner: Record<string, unknown>, cursor = '7-1'): string {
  return `event: agent_event\ndata: ${JSON.stringify({ type: 'event', event: inner, cursor })}\n\n`
}

interface NativeStreamArgs {
  issuer?: string
  urlParams?: Record<string, string>
  channel?: { onmessage?: (frame: unknown) => void }
  requestId?: string
}

function streamInvokeArgs(): NativeStreamArgs {
  const call = invoke.mock.calls.find(([command]) => command === 'native_agent_stream')
  return (call?.[1] ?? {}) as NativeStreamArgs
}

function cancelCalls(): unknown[] {
  return invoke.mock.calls
    .filter(([command]) => command === 'native_agent_stream_cancel')
    .map(([, args]) => args)
}

function harness(request: AgentStreamConnectRequest = {}) {
  const events: AgentStreamTransportEvent<AguiEvent>[] = []
  const transport = createTauriAgentStreamTransport({ wire: 'agui' })
  const pending = transport.connect(request, (event) => { events.push(event) })
  return { events, pending }
}

describe('createTauriAgentStreamTransport', () => {
  beforeEach(() => {
    invoke.mockReset()
    logNativeEvent.mockReset()
    serverConfig.current = 'https://b.example'
  })

  it('connects with the issuer, camelCase urlParams, a channel, and a fresh UUID requestId', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const { pending } = harness({ conversationId: 'conv-1', cursor: '7-3' })

    await pending

    expect(invoke).toHaveBeenCalledTimes(1)
    const args = streamInvokeArgs()
    expect(args.issuer).toBe('https://b.example')
    expect(args.urlParams).toEqual({ conversationId: 'conv-1', cursor: '7-3' })
    expect(args.requestId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    expect(typeof args.channel?.onmessage).toBe('function')
  })

  it('omits optional urlParams fields for a global stream subscription', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const { pending } = harness()

    await pending

    expect(streamInvokeArgs().urlParams).toEqual({})
  })

  it('forwards the three channel frame shapes to the transport events', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const { events, pending } = harness()
    await pending
    const onmessage = streamInvokeArgs().channel?.onmessage?.bind(null)

    onmessage?.({ type: 'open' })
    onmessage?.({ type: 'event', data: agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'hi' }, '7-1') })
    onmessage?.({ type: 'close', code: 4410, reason: 'too_slow' })

    expect(events).toEqual([
      { type: 'open' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'hi' }, cursor: '7-1' },
      { type: 'close', code: 4410, reason: 'too_slow' },
    ])
  })

  it('parses real agui frame samples through parseAgentStreamFrameAgui', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const { events, pending } = harness()
    await pending
    const onmessage = streamInvokeArgs().channel?.onmessage?.bind(null)

    onmessage?.({
      type: 'event',
      data: `event: agent_event\ndata: ${JSON.stringify({ type: 'event', event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' }, cursor: '7-1' })}\n\n`,
    })
    onmessage?.({ type: 'event', data: 'event: reset\ndata: {"type":"reset","cursor":"8-0"}\n\n' })
    onmessage?.({ type: 'event', data: 'event: closed\ndata: {"type":"closed","code":4412,"reason":"binding_revoked"}\n\n' })

    expect(events).toEqual([
      { type: 'event', event: { type: 'RUN_STARTED', threadId: 'c', runId: 'r' }, cursor: '7-1' },
      { type: 'reset', cursor: '8-0' },
      { type: 'close', code: 4412, reason: 'binding_revoked' },
    ])
  })

  it('silently drops malformed and unknown event frames without logging', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { events, pending } = harness()
    await pending
    const onmessage = streamInvokeArgs().channel?.onmessage?.bind(null)

    onmessage?.({ type: 'event', data: 'event: agent_event\ndata: {broken json}\n\n' })
    onmessage?.({ type: 'event', data: 'event: agent_event\ndata: {"type":"event","event":{"type":"NOPE"},"cursor":"7-1"}\n\n' })
    onmessage?.({ type: 'event', data: agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'ok' }, '7-1') })

    expect(events).toEqual([
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'ok' }, cursor: '7-1' },
    ])
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    log.mockRestore()
    error.mockRestore()
  })

  it('ignores every channel frame after the terminal close frame', async () => {
    invoke.mockImplementation(() => new Promise<void>(() => {}))
    const { events, pending } = harness()
    await pending
    const onmessage = streamInvokeArgs().channel?.onmessage?.bind(null)

    onmessage?.({ type: 'open' })
    onmessage?.({ type: 'event', data: agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'x' }, '7-1') })
    onmessage?.({ type: 'close', code: 4401, reason: 'authentication_required' })
    onmessage?.({ type: 'open' })
    onmessage?.({ type: 'event', data: agentEventFrame({ type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'late' }, '7-2') })
    onmessage?.({ type: 'close', code: 1006, reason: 'late' })

    expect(events).toEqual([
      { type: 'open' },
      { type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: 'x' }, cursor: '7-1' },
      { type: 'close', code: 4401, reason: 'authentication_required' },
    ])
  })

  it('emits a 1006 transport_error close when the command settles without one', async () => {
    invoke.mockResolvedValue(undefined)
    const { events, pending } = harness()
    await pending

    await vi.waitFor(() => {
      expect(events).toEqual([{ type: 'close', code: 1006, reason: 'transport_error' }])
    })
  })

  it('never emits a second close frame after the settle handler ran', async () => {
    invoke.mockResolvedValue(undefined)
    const { events, pending } = harness()
    await pending
    await vi.waitFor(() => {
      expect(events.some((event) => event.type === 'close')).toBe(true)
    })

    streamInvokeArgs().channel?.onmessage?.({ type: 'close', code: 4401, reason: 'authentication_required' })

    expect(events).toEqual([{ type: 'close', code: 1006, reason: 'transport_error' }])
  })

  it('maps a command rejection to a 1006 close carrying the safe error text', async () => {
    invoke.mockRejectedValue('offline')
    const { events, pending } = harness()
    await pending

    await vi.waitFor(() => {
      expect(events).toEqual([{ type: 'close', code: 1006, reason: 'offline' }])
    })
    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'agent_stream_failed', errorCode: 'offline' }))
  })

  it('sanitizes credential-shaped rejection text before it reaches the close reason', async () => {
    invoke.mockRejectedValue(new Error('fetch https://b.example/api failed access_token=abc123'))
    const { events, pending } = harness()
    await pending

    await vi.waitFor(() => {
      expect(events).toHaveLength(1)
    })
    const close = events[0]
    if (close === undefined || close.type !== 'close') throw new Error('expected a close frame')
    expect(close).toMatchObject({ type: 'close', code: 1006 })
    expect(close.reason).toContain('<url>')
    expect(close.reason).toContain('<redacted>')
    expect(close.reason).not.toContain('abc123')
    expect(close.reason).not.toContain('b.example')
  })

  it('close invokes the idempotent cancel command with the connect requestId and stays silent', async () => {
    invoke.mockImplementation((command: string) => {
      if (command === 'native_agent_stream_cancel') return Promise.resolve(undefined)
      return new Promise<void>(() => {})
    })
    const { events, pending } = harness()
    const connection: AgentStreamConnection = await pending
    const requestId = streamInvokeArgs().requestId

    await connection.close(1000, 'client_closed')
    await connection.close(1000, 'client_closed')

    expect(cancelCalls()).toEqual([{ requestId }, { requestId }])
    // An explicit close is silent at the call site: the terminal frame is
    // owned by the command's settle handler, which is still pending here.
    expect(events).toEqual([])
  })

  it('close swallows a failed cancel instead of throwing or emitting a close frame', async () => {
    invoke.mockImplementation((command: string) => {
      if (command === 'native_agent_stream_cancel') return Promise.reject(new Error('ipc down'))
      return new Promise<void>(() => {})
    })
    const { events, pending } = harness()
    const connection = await pending

    await expect(connection.close(1000, 'client_closed')).resolves.toBeUndefined()

    expect(logNativeEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'agent_stream_cancel_failed' }))
    expect(events).toEqual([])
  })

  it('rejects an unsupported wire at construction', () => {
    expect(() => createTauriAgentStreamTransport({ wire: 'canonical' as never })).toThrow('unsupported agent stream wire')
  })
})
