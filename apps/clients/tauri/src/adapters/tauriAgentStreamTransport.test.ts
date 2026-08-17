import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentStreamConnectRequest, AgentStreamTransportEvent, AguiEvent } from '@termflow/client-core'

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
})
