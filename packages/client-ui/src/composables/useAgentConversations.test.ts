import { defineComponent, h, ref, type Ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import type { AgentConversationResponse, ApprovalResponse } from '@termflow/client-contracts'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeAgentCursorStore, createFakeRuntime } from '../test/fakeRuntime'
import { useAgentConversations } from './useAgentConversations'
import { useBottomToast, type BottomToastController } from './useBottomToast'

function conversation(id: string, overrides: Partial<AgentConversationResponse> = {}): AgentConversationResponse {
  return {
    conversation_id: id,
    binding_id: 'binding-1',
    title: null,
    status: 'open',
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
    ...overrides,
  }
}

function pendingApproval(conversationId: string, approvalId: string): ApprovalResponse {
  return {
    approval_id: approvalId,
    binding_id: 'binding-1',
    conversation_id: conversationId,
    run_id: null,
    tool_call_id: `tool-${approvalId}`,
    canonical_hash: `hash-${approvalId}`,
    state: 'pending',
    expires_at: '2026-08-13T00:00:00+00:00',
    decided_at: null,
    decision: null,
    auth_epoch: 7,
    created_at: '2026-08-12T00:00:00+00:00',
    pane_id: null,
    operation: null,
    intent_summary: null,
  }
}

interface ConversationsHarness {
  conversations(): ReturnType<typeof useAgentConversations>
  toast(): BottomToastController
  cursorStore: ReturnType<typeof createFakeAgentCursorStore>
  listConversations: ReturnType<typeof vi.fn>
  createConversation: ReturnType<typeof vi.fn>
  deleteConversation: ReturnType<typeof vi.fn>
  request: ReturnType<typeof vi.fn>
  unmount(): void
}

function mountConversations(overrides: {
  bindingId?: string
  /** Reactive binding source: the AgentView selector switches this ref. */
  bindingRef?: Ref<string | undefined>
  listConversations?: ReturnType<typeof vi.fn>
  createConversation?: ReturnType<typeof vi.fn>
  deleteConversation?: ReturnType<typeof vi.fn>
  request?: ReturnType<typeof vi.fn>
  cursorStore?: ReturnType<typeof createFakeAgentCursorStore>
} = {}): ConversationsHarness {
  const listConversations = overrides.listConversations ?? vi.fn(async () => ({ conversations: [] }))
  const createConversation = overrides.createConversation ?? vi.fn(async () => conversation('conv-created'))
  const deleteConversation = overrides.deleteConversation ?? vi.fn(async () => undefined)
  const request = overrides.request ?? vi.fn(async () => ({ approvals: [] }))
  const cursorStore = overrides.cursorStore ?? createFakeAgentCursorStore()
  const runtime = createFakeRuntime({
    agentCursorStore: cursorStore,
    api: {
      ...createFakeRuntime().api,
      agents: {
        listConversations,
        createConversation,
        deleteConversation,
        capabilities: vi.fn(async () => ({ agent_broker_enabled: true, delegated_write_grants_enabled: false, speech_to_text_enabled: false })),
        listMessages: vi.fn(async () => ({ messages: [] })),
        getConversation: vi.fn(async () => ({})) as never,
        listEvents: vi.fn(async () => ({ events: [] })) as never,
        submitMessage: vi.fn(async () => ({})) as never,
        cancelRun: vi.fn(async () => ({})) as never,
      },
      request,
    } as unknown as ClientRuntime['api'],
  })
  let toast: BottomToastController | undefined
  let state: ReturnType<typeof useAgentConversations> | undefined
  const wrapper = mount(defineComponent({
    setup() {
      toast = useBottomToast()
      state = useAgentConversations(overrides.bindingRef ?? overrides.bindingId)
      return () => h('div')
    },
  }), { global: { plugins: [createClientUi(runtime)] } })
  return {
    conversations: () => {
      if (state === undefined) throw new Error('conversations not captured')
      return state
    },
    toast: () => {
      if (toast === undefined) throw new Error('toast not captured')
      return toast
    },
    cursorStore,
    listConversations,
    createConversation,
    deleteConversation,
    request,
    unmount: () => wrapper.unmount(),
  }
}

async function mounted(overrides: Parameters<typeof mountConversations>[0] = {}) {
  const harness = mountConversations(overrides)
  await flushPromises()
  return harness
}

describe('useAgentConversations', () => {
  it('lists conversations scoped by binding and groups pending approval badges per conversation', async () => {
    const listConversations = vi.fn(async () => ({ conversations: [conversation('conv-1'), conversation('conv-2')] }))
    const request = vi.fn(async (_path: string) => ({
      approvals: [
        pendingApproval('conv-1', 'a1'),
        pendingApproval('conv-1', 'a2'),
        pendingApproval('conv-2', 'a3'),
        { ...pendingApproval('conv-1', 'a4'), state: 'approved' },
      ],
    }))
    const harness = await mounted({ bindingId: 'binding-1', listConversations, request })

    expect(listConversations).toHaveBeenCalledWith(expect.objectContaining({ bindingId: 'binding-1' }))
    expect(request.mock.calls[0]?.[0]).toBe('/api/v1/agent/approvals')
    expect(harness.conversations().conversations.value).toHaveLength(2)
    expect(harness.conversations().pendingCount('conv-1')).toBe(2)
    expect(harness.conversations().pendingCount('conv-2')).toBe(1)
    expect(harness.conversations().pendingCount('conv-3')).toBe(0)
  })

  it('creates a conversation and prepends it with a toast', async () => {
    const created = conversation('conv-new', { title: '新会话' })
    const harness = await mounted({ createConversation: vi.fn(async () => created) })

    const result = await harness.conversations().create('binding-1', '新会话')
    expect(result).toEqual(created)
    expect(harness.createConversation).toHaveBeenCalledWith({ binding_id: 'binding-1', title: '新会话' })
    expect(harness.conversations().conversations.value[0]).toEqual(created)
    expect(harness.toast().current.value?.text).toBe('会话已创建。')
  })

  it('deletes a conversation, drops it from the list, and clears its cursor', async () => {
    const listConversations = vi.fn(async () => ({ conversations: [conversation('conv-1'), conversation('conv-2')] }))
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save('conv-1', '7-9', 9)
    const harness = await mounted({ bindingId: 'binding-1', listConversations, cursorStore })

    await harness.conversations().remove('conv-1')
    expect(harness.deleteConversation).toHaveBeenCalledWith('conv-1', expect.any(AbortSignal))
    expect(harness.conversations().conversations.value.map((entry) => entry.conversation_id)).toEqual(['conv-2'])
    expect(harness.cursorStore.load('conv-1')).toBeNull()
    expect(harness.toast().current.value?.text).toBe('会话已删除。')
  })

  it('toasts when the list fails to load', async () => {
    const harness = await mounted({ bindingId: 'binding-1', listConversations: vi.fn(async () => {
      throw new Error('boom')
    }) })
    expect(harness.conversations().loading.value).toBe(false)
    expect(harness.toast().current.value?.text).toBe('无法加载会话列表。')
  })

  it('re-scopes the list and badges when the reactive binding switches', async () => {
    const bindingRef = ref<string | undefined>(undefined)
    const listConversations = vi.fn()
      .mockResolvedValueOnce({ conversations: [conversation('conv-a')] })
      .mockResolvedValueOnce({ conversations: [conversation('conv-b')] })
    const request = vi.fn()
      .mockResolvedValueOnce({ approvals: [pendingApproval('conv-a', 'a1')] })
      .mockResolvedValueOnce({ approvals: [] })
    const harness = mountConversations({ bindingRef, listConversations, request })
    await flushPromises()

    // No binding selected yet: the fetch is skipped entirely.
    expect(listConversations).not.toHaveBeenCalled()
    expect(harness.conversations().loading.value).toBe(false)
    expect(harness.conversations().conversations.value).toEqual([])

    bindingRef.value = 'binding-1'
    await flushPromises()
    expect(listConversations).toHaveBeenCalledTimes(1)
    expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ bindingId: 'binding-1' }))
    expect(harness.conversations().conversations.value.map((entry) => entry.conversation_id)).toEqual(['conv-a'])
    expect(harness.conversations().pendingCount('conv-a')).toBe(1)

    bindingRef.value = 'binding-2'
    await flushPromises()
    expect(listConversations).toHaveBeenCalledTimes(2)
    expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ bindingId: 'binding-2' }))
    expect(harness.conversations().conversations.value.map((entry) => entry.conversation_id)).toEqual(['conv-b'])
    expect(harness.conversations().pendingCount('conv-a')).toBe(0)
    harness.unmount()
  })
})
