import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { AgentConversationResponse } from '@termflow/client-contracts'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime, type ClientUiPlugin } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import { useAgentConversations } from './useAgentConversations'

function conversation(conversationId: string): AgentConversationResponse {
  return {
    conversation_id: conversationId,
    binding_id: 'b1',
    title: null,
    status: 'open',
    write_policy: 'manual',
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
  }
}

interface Deferred<T> extends Promise<T> {
  resolve(value: T): void
  reject(error: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  }) as Deferred<T>
  promise.resolve = resolve
  promise.reject = reject
  return promise
}

interface ConversationsHarness {
  wrapper: VueWrapper
  clientUi: ClientUiPlugin
  bindingId: ReturnType<typeof ref<string | string[] | undefined>>
  state(): ReturnType<typeof useAgentConversations>
}

async function mounted(overrides: {
  bindingId?: string | string[]
  listConversations?: ReturnType<typeof vi.fn>
  deleteConversation?: ReturnType<typeof vi.fn>
  createConversation?: ReturnType<typeof vi.fn>
  renameConversation?: ReturnType<typeof vi.fn>
} = {}): Promise<ConversationsHarness> {
  const bindingId = ref<string | string[] | undefined>(overrides.bindingId ?? 'b1')
  const listConversations = overrides.listConversations ?? vi.fn(async () => ({ conversations: [] }))
  const deleteConversation = overrides.deleteConversation ?? vi.fn(async () => undefined)
  const createConversation = overrides.createConversation ?? vi.fn(async () => ({})) as never
  const renameConversation = overrides.renameConversation ?? vi.fn(async (_id: string, title: string) => ({ ...conversation('renamed'), title }))
  const runtime = createFakeRuntime({
    api: {
      ...createFakeRuntime().api,
      agents: {
        ...createFakeRuntime().api.agents,
        listConversations,
        deleteConversation,
        createConversation,
        renameConversation,
      },
      request: vi.fn(async () => ({ approvals: [] })),
    } as unknown as ClientRuntime['api'],
  })
  const clientUi = createClientUi(runtime)
  let exposed!: ReturnType<typeof useAgentConversations>
  const harness = defineComponent({
    setup() {
      exposed = useAgentConversations(bindingId)
      return {}
    },
    render: () => null,
  })
  const wrapper = mount(harness, { attachTo: document.body, global: { plugins: [clientUi] } })
  await flushPromises()
  return { wrapper, clientUi, bindingId, state: () => exposed }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('useAgentConversations', () => {
  it('does not abort an in-flight delete when a binding switch reloads the list', async () => {
    const deleteDeferred = deferred<void>()
    const deleteConversation = vi.fn((_conversationId: string, _signal?: AbortSignal) => deleteDeferred)
    const listCalls: Array<Deferred<{ conversations: AgentConversationResponse[] }>> = []
    const listSignals: AbortSignal[] = []
    const listConversations = vi.fn((options: { bindingId?: string, signal?: AbortSignal }) => {
      listSignals.push(options.signal as AbortSignal)
      const pending = deferred<{ conversations: AgentConversationResponse[] }>()
      listCalls.push(pending)
      return pending
    })
    const harness = await mounted({ listConversations, deleteConversation })
    listCalls[0]!.resolve({ conversations: [conversation('c1')] })
    await flushPromises()
    expect(harness.state().conversations.value.map((entry) => entry.conversation_id)).toEqual(['c1'])

    // The delete is in flight (mutation controller signal). Switching the
    // binding re-scopes the list and aborts the old LIST request — it must
    // not cancel the delete.
    const removePromise = harness.state().remove('c1')
    harness.bindingId.value = 'b2'
    await flushPromises()

    // The old list request was aborted (superseded scope)…
    expect(listSignals[0]?.aborted).toBe(true)
    // …but the delete request's signal is untouched.
    expect(deleteConversation.mock.calls[0]?.[1]?.aborted).toBe(false)

    deleteDeferred.resolve(undefined)
    await removePromise
    expect(harness.clientUi.toast.current.value?.text).toBe('会话已删除。')
    // The deleted conversation drops out of the local list.
    expect(harness.state().conversations.value.map((entry) => entry.conversation_id)).toEqual([])
    harness.wrapper.unmount()
  })

  it('does not abort an in-flight create when a binding switch reloads the list', async () => {
    const createDeferred = deferred<AgentConversationResponse>()
    const createConversation = vi.fn((_body: unknown, _signal?: AbortSignal) => createDeferred)
    const listCalls: Array<Deferred<{ conversations: AgentConversationResponse[] }>> = []
    const listConversations = vi.fn(() => {
      const pending = deferred<{ conversations: AgentConversationResponse[] }>()
      listCalls.push(pending)
      return pending
    })
    const harness = await mounted({ listConversations, createConversation })
    listCalls[0]!.resolve({ conversations: [] })
    await flushPromises()

    const createPromise = harness.state().create('b1', '新会话')
    harness.bindingId.value = 'b2'
    await flushPromises()

    expect(createConversation.mock.calls[0]?.[1]?.aborted).toBe(false)
    createDeferred.resolve(conversation('c2'))
    await createPromise
    expect(harness.clientUi.toast.current.value?.text).toBe('会话已创建。')
    expect(harness.state().conversations.value.map((entry) => entry.conversation_id)).toEqual(['c2'])
    harness.wrapper.unmount()
  })

  it('aggregates conversation rows across all binding scopes', async () => {
    const listConversations = vi.fn(async ({ bindingId }: { bindingId?: string }) => ({
      conversations: [conversation(bindingId === 'b1' ? 'c1' : 'c2')],
    }))
    const harness = await mounted({ bindingId: ['b1', 'b2'], listConversations })

    expect(listConversations).toHaveBeenCalledWith(expect.objectContaining({ bindingId: 'b1' }))
    expect(listConversations).toHaveBeenCalledWith(expect.objectContaining({ bindingId: 'b2' }))
    expect(harness.state().conversations.value.map((entry) => entry.conversation_id)).toEqual(['c1', 'c2'])
    harness.wrapper.unmount()
  })

  it('renames a conversation through the API and updates its local row', async () => {
    const renameConversation = vi.fn(async (_id: string, title: string) => ({ ...conversation('c1'), title }))
    const harness = await mounted({
      renameConversation,
      listConversations: vi.fn(async () => ({ conversations: [conversation('c1')] })),
    })
    await harness.state().rename('c1', '新标题')

    expect(renameConversation).toHaveBeenCalledWith('c1', '新标题', expect.anything())
    expect(harness.state().conversations.value[0]?.title).toBe('新标题')
    harness.wrapper.unmount()
  })
})
