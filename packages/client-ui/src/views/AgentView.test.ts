import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ApiError } from '@termflow/client-core'
import type { AgentBindingResponse, AgentConversationResponse, ApprovalResponse } from '@termflow/client-contracts'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeAgentCursorStore, createFakeRuntime } from '../test/fakeRuntime'
import AgentView from './AgentView.vue'

function binding(id: string, termId: string, profileId: string, status: string): AgentBindingResponse {
  return {
    binding_id: id,
    profile_id: profileId,
    term_id: termId,
    status,
    runtime_ref: null,
    runtime_epoch: null,
    capability_ref: null,
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
  }
}

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

interface AgentViewHarness {
  wrapper: VueWrapper
  listBindings: ReturnType<typeof vi.fn>
  listConversations: ReturnType<typeof vi.fn>
  createConversation: ReturnType<typeof vi.fn>
  deleteConversation: ReturnType<typeof vi.fn>
  request: ReturnType<typeof vi.fn>
  cursorStore: ReturnType<typeof createFakeAgentCursorStore>
  toast(): { text: string | null }
}

function mountAgentView(overrides: {
  capabilities?: ReturnType<typeof vi.fn>
  listBindings?: ReturnType<typeof vi.fn>
  listConversations?: ReturnType<typeof vi.fn>
  createConversation?: ReturnType<typeof vi.fn>
  deleteConversation?: ReturnType<typeof vi.fn>
  request?: ReturnType<typeof vi.fn>
  cursorStore?: ReturnType<typeof createFakeAgentCursorStore>
} = {}): AgentViewHarness {
  const capabilities = overrides.capabilities ?? vi.fn(async () => ({
    agent_broker_enabled: true,
    delegated_write_grants_enabled: false,
  }))
  const listBindings = overrides.listBindings ?? vi.fn(async () => ({
    bindings: [binding('b1', 'term-1', 'profile-1', 'ready'), binding('b2', 'term-2', 'profile-2', 'offline')],
  }))
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
        capabilities,
        listBindings,
        listConversations,
        createConversation,
        deleteConversation,
        getConversation: vi.fn(async () => ({})) as never,
        listMessages: vi.fn(async () => ({ messages: [] })),
        listEvents: vi.fn(async () => ({ events: [] })) as never,
        submitMessage: vi.fn(async () => ({})) as never,
        cancelRun: vi.fn(async () => ({})) as never,
      },
      request,
    } as unknown as ClientRuntime['api'],
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agent', component: AgentView },
      { path: '/agent/:conversationId', component: { template: '<div />' } },
    ],
  })
  const clientUi = createClientUi(runtime)
  const wrapper = mount(AgentView, {
    attachTo: document.body,
    global: { plugins: [router, clientUi] },
  })
  return {
    wrapper,
    listBindings,
    listConversations,
    createConversation,
    deleteConversation,
    request,
    cursorStore,
    toast: () => ({ text: clientUi.toast.current.value?.text ?? null }),
  }
}

async function mounted(overrides: Parameters<typeof mountAgentView>[0] = {}) {
  const harness = mountAgentView(overrides)
  await flushPromises()
  return harness
}

async function selectFirstBinding(harness: AgentViewHarness) {
  await harness.wrapper.get('.agent-binding-option').trigger('click')
  await flushPromises()
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AgentView', () => {
  it('renders the disabled placeholder and skips the binding fetch when the broker is disabled', async () => {
    const harness = await mounted({
      capabilities: vi.fn(async () => ({ agent_broker_enabled: false, delegated_write_grants_enabled: false })),
    })

    expect(harness.wrapper.get('[data-agent-disabled]').text()).toContain('Agent Broker 未启用')
    expect(harness.listBindings).not.toHaveBeenCalled()
    expect(harness.wrapper.find('.agent-binding-option').exists()).toBe(false)
  })

  it('lists bindings with term/profile/status and scopes conversations on selection', async () => {
    const harness = await mounted()
    const options = harness.wrapper.findAll('.agent-binding-option')
    expect(options).toHaveLength(2)
    expect(options[0]!.text()).toContain('Term term-1')
    expect(options[0]!.text()).toContain('Profile profile-1')
    expect(options[0]!.get('[data-agent-binding-status]').text()).toBe('ready')
    expect(harness.wrapper.get('[data-agent-grants]').text()).toContain('Delegated Write Grants：不可用')

    // No binding selected yet: hint shown, no conversation fetch.
    expect(harness.wrapper.find('[data-agent-conversations-need-binding]').exists()).toBe(true)
    expect(harness.listConversations).not.toHaveBeenCalled()

    await options[0]!.trigger('click')
    await flushPromises()
    expect(harness.listConversations).toHaveBeenCalledWith(expect.objectContaining({ bindingId: 'b1' }))
    expect(harness.wrapper.find('[data-agent-conversations-need-binding]').exists()).toBe(false)
    expect(options[0]!.attributes('aria-pressed')).toBe('true')
  })

  it('renders conversations with a title fallback and pending approval badges', async () => {
    const listConversations = vi.fn(async () => ({
      conversations: [conversation('11111111-aaaa'), conversation('22222222-bbbb', { title: '我的会话' })],
    }))
    const request = vi.fn(async () => ({
      approvals: [
        pendingApproval('11111111-aaaa', 'a1'),
        pendingApproval('11111111-aaaa', 'a2'),
        pendingApproval('22222222-bbbb', 'a3'),
        { ...pendingApproval('11111111-aaaa', 'a4'), state: 'approved' },
      ],
    }))
    const harness = await mounted({ listConversations, request })
    await selectFirstBinding(harness)

    const rows = harness.wrapper.findAll('.agent-conversation-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('会话 11111111')
    expect(rows[1]!.text()).toContain('我的会话')
    expect(rows[0]!.get('[data-agent-pending-badge]').text()).toBe('2')
    expect(rows[1]!.get('[data-agent-pending-badge]').text()).toBe('1')
    expect(rows[0]!.get('a').attributes('href')).toBe('/agent/11111111-aaaa')
  })

  it('creates a conversation for the selected binding with an optional title', async () => {
    const created = conversation('conv-new', { title: '新会话' })
    const harness = await mounted({ createConversation: vi.fn(async () => created) })
    await selectFirstBinding(harness)

    await harness.wrapper.get('[data-agent-create-title]').setValue('新会话')
    await harness.wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(harness.createConversation).toHaveBeenCalledWith({ binding_id: 'b1', title: '新会话' })
    expect((harness.wrapper.get('[data-agent-create-title]').element as HTMLInputElement).value).toBe('')
    expect(harness.wrapper.findAll('.agent-conversation-row')).toHaveLength(1)
    expect(harness.wrapper.text()).toContain('新会话')
  })

  it('deletes a conversation through a focus-trapped confirm dialog', async () => {
    const listConversations = vi.fn(async () => ({ conversations: [conversation('conv-1'), conversation('conv-2')] }))
    const cursorStore = createFakeAgentCursorStore()
    cursorStore.save('conv-1', '7-9', 9)
    const harness = await mounted({ listConversations, cursorStore })
    await selectFirstBinding(harness)

    await harness.wrapper.findAll('[data-action="delete-conversation"]')[0]!.trigger('click')
    await nextTick()
    const dialog = harness.wrapper.get('[data-agent-delete-dialog]')
    expect(dialog.text()).toContain('会话 conv-1')
    // Initial focus lands on the cancel button (ClosePaneDialog pattern).
    expect(document.activeElement?.textContent).toContain('取消')

    // Escape cancels without deleting.
    await dialog.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(harness.wrapper.find('[data-agent-delete-dialog]').exists()).toBe(false)
    expect(harness.deleteConversation).not.toHaveBeenCalled()
    expect(harness.wrapper.findAll('.agent-conversation-row')).toHaveLength(2)

    // Re-open and confirm: DELETE goes out, row drops, cursor cleared.
    await harness.wrapper.findAll('[data-action="delete-conversation"]')[0]!.trigger('click')
    await harness.wrapper.get('[data-action="delete-confirm"]').trigger('click')
    await flushPromises()
    expect(harness.deleteConversation).toHaveBeenCalledWith('conv-1', expect.any(AbortSignal))
    expect(harness.wrapper.findAll('.agent-conversation-row')).toHaveLength(1)
    expect(harness.cursorStore.load('conv-1')).toBeNull()
  })

  it('shows an error state and toast when the binding list fails to load', async () => {
    const harness = await mounted({ listBindings: vi.fn(async () => {
      throw new Error('boom')
    }) })

    expect(harness.wrapper.get('[data-agent-bindings-failed]').text()).toContain('无法加载 Binding 列表')
    expect(harness.toast().text).toBe('无法加载 Binding 列表。')
  })

  it('does not surface a navigation-abort as a binding failure or toast', async () => {
    const harness = await mounted({ listBindings: vi.fn(async () => {
      throw new ApiError('aborted')
    }) })

    expect(harness.wrapper.find('[data-agent-bindings-failed]').exists()).toBe(false)
    expect(harness.toast().text).toBeNull()
  })

  it('shows an empty state when no bindings exist', async () => {
    const harness = await mounted({ listBindings: vi.fn(async () => ({ bindings: [] })) })
    expect(harness.wrapper.get('[data-agent-bindings-empty]').text()).toContain('无可用 Binding')
  })
})
