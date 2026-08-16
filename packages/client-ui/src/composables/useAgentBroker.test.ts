import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { ApiError } from '@termflow/client-core'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import { useAgentBroker } from './useAgentBroker'

function mountBroker(capabilities: ReturnType<typeof vi.fn>) {
  const runtime = createFakeRuntime({
    api: {
      ...createFakeRuntime().api,
      agents: {
        capabilities,
        listMessages: vi.fn(async () => ({ messages: [] })),
        listConversations: vi.fn(async () => ({ conversations: [] })),
        createConversation: vi.fn(async () => ({})) as never,
        getConversation: vi.fn(async () => ({})) as never,
        listEvents: vi.fn(async () => ({ events: [] })) as never,
        submitMessage: vi.fn(async () => ({})) as never,
        cancelRun: vi.fn(async () => ({})) as never,
        deleteConversation: vi.fn(async () => undefined) as never,
      },
    } as unknown as ClientRuntime['api'],
  })
  let broker: ReturnType<typeof useAgentBroker> | undefined
  const wrapper = mount(defineComponent({
    setup() {
      broker = useAgentBroker()
      return () => h('div')
    },
  }), { global: { plugins: [createClientUi(runtime)] } })
  return {
    broker: () => {
      if (broker === undefined) throw new Error('broker not captured')
      return broker
    },
    capabilities,
    unmount: () => wrapper.unmount(),
  }
}

async function mounted(capabilities: ReturnType<typeof vi.fn>) {
  const harness = mountBroker(capabilities)
  await flushPromises()
  return harness
}

describe('useAgentBroker', () => {
  it('exposes the capability gate from a single fetch', async () => {
    const capabilities = vi.fn(async () => ({ agent_broker_enabled: true, delegated_write_grants_enabled: false }))
    const harness = await mounted(capabilities)

    expect(capabilities).toHaveBeenCalledTimes(1)
    expect(harness.broker().agentBrokerEnabled.value).toBe(true)
    expect(harness.broker().delegatedWriteGrantsEnabled.value).toBe(false)
    expect(harness.broker().loading.value).toBe(false)
    expect(harness.broker().failed.value).toBe(false)
  })

  it('keeps the gate closed when the broker is disabled', async () => {
    const harness = await mounted(vi.fn(async () => ({ agent_broker_enabled: false, delegated_write_grants_enabled: false })))
    expect(harness.broker().agentBrokerEnabled.value).toBe(false)
  })

  it('fails closed when the capabilities fetch errors', async () => {
    const harness = await mounted(vi.fn(async () => {
      throw new ApiError('offline')
    }))
    expect(harness.broker().agentBrokerEnabled.value).toBe(false)
    expect(harness.broker().failed.value).toBe(true)
    expect(harness.broker().loading.value).toBe(false)
  })

  it('reloads the capability on demand', async () => {
    const capabilities = vi.fn()
      .mockResolvedValueOnce({ agent_broker_enabled: false, delegated_write_grants_enabled: false })
      .mockResolvedValueOnce({ agent_broker_enabled: true, delegated_write_grants_enabled: false })
    const harness = await mounted(capabilities)
    expect(harness.broker().agentBrokerEnabled.value).toBe(false)

    await harness.broker().reload()
    expect(harness.broker().agentBrokerEnabled.value).toBe(true)
    expect(capabilities).toHaveBeenCalledTimes(2)
  })
})
