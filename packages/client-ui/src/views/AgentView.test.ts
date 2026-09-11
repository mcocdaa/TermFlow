import { flushPromises, mount } from '@vue/test-utils'
import type { AgentBindingResponse, AgentConversationResponse } from '@termflow/client-contracts'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import AgentView from './AgentView.vue'

const binding: AgentBindingResponse = {
  binding_id: 'binding-1',
  profile_id: 'profile-1',
  term_id: 'term-1',
  status: 'ready',
  write_policy: 'manual',
  runtime_ref: 'runtime-1',
  runtime_epoch: 1,
  capability_ref: 'capability-1',
  created_at: '2026-09-09T00:00:00Z',
  updated_at: '2026-09-09T00:00:00Z',
}

const conversation: AgentConversationResponse = {
  conversation_id: 'conversation-1',
  binding_id: binding.binding_id,
  title: '部署检查',
  status: 'active',
  created_at: '2026-09-09T00:00:00Z',
  updated_at: '2026-09-09T00:00:00Z',
}

function completeComputerList() {
  return {
    computers: [{
      installation_id: 'computer-1',
      hostname: 'host-1',
      display_name: '电脑一',
      platform: 'linux',
      client_version: '0.2.0',
      registered_at: '2026-09-09T00:00:00Z',
      last_seen_at: '2026-09-09T00:00:00Z',
      online: true,
      terms: [{
        instance_id: binding.term_id,
        name: '开发 Term',
        online: true,
        window_count: 1,
        pane_count: 1,
        active_pane_count: 1,
        current_command: 'zsh',
        last_seen_at: '2026-09-09T00:00:00Z',
      }],
    }],
  }
}

async function mounted(overrides: {
  listBindings?: ReturnType<typeof vi.fn>
  listConversations?: ReturnType<typeof vi.fn>
  renameConversation?: ReturnType<typeof vi.fn>
  request?: ReturnType<typeof vi.fn>
} = {}) {
  const base = createFakeRuntime()
  const runtime = createFakeRuntime({
    api: {
      ...base.api,
      request: overrides.request ?? vi.fn(async () => ({ approvals: [] })),
      computers: {
        ...base.api.computers,
        list: vi.fn(async () => completeComputerList()),
      },
      agents: {
        ...base.api.agents,
        capabilities: vi.fn(async () => ({
          agent_broker_enabled: true,
          delegated_write_grants_enabled: false,
          state: 'ready',
          reason_code: null,
        })),
        listBindings: overrides.listBindings ?? vi.fn(async () => ({ bindings: [binding] })),
        listConversations: overrides.listConversations ?? vi.fn(async () => ({ conversations: [conversation] })),
        createConversation: vi.fn(async () => conversation),
        renameConversation: overrides.renameConversation ?? vi.fn(async (_id: string, title: string) => ({ ...conversation, title })),
        deleteConversation: vi.fn(async () => undefined),
      },
    } as unknown as ClientRuntime['api'],
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agent', component: AgentView },
      { path: '/agent/:conversationId', component: { template: '<div />' } },
    ],
  })
  await router.push('/agent')
  await router.isReady()
  const wrapper = mount(AgentView, {
    attachTo: document.body,
    global: { plugins: [router, createClientUi(runtime)] },
  })
  await flushPromises()
  return { wrapper, runtime }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AgentView', () => {
  it('presents conversations as a Term table without exposing Binding internals or grants text', async () => {
    const { wrapper } = await mounted()

    expect(wrapper.find('[data-agent-create-title]').exists()).toBe(false)
    expect(wrapper.find('[data-action="create-conversation"]').exists()).toBe(false)
    expect(wrapper.get('[data-agent-conversation-table]').text()).toContain('会话名称')
    expect(wrapper.get('[data-agent-conversation-table]').text()).toContain('Term')
    expect(wrapper.get('[data-agent-conversation-table]').text()).toContain('工作状态')
    expect(wrapper.get('[data-agent-conversation-table]').text()).toContain('操作')
    expect(wrapper.text()).toContain('开发 Term')
    expect(wrapper.text()).toContain('运行中')
    expect(wrapper.text()).not.toContain('选择 Binding')
    expect(wrapper.text()).not.toContain('Delegated Write Grants')
    expect(wrapper.find('[data-agent-binding-id]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renames a conversation from its row and reflects the returned title', async () => {
    const renameConversation = vi.fn(async (_id: string, title: string) => ({ ...conversation, title }))
    const { wrapper } = await mounted({ renameConversation })

    await wrapper.get('[data-action="rename-conversation"]').trigger('click')
    expect(document.activeElement).toBe(wrapper.get('[data-agent-rename-input]').element)
    await wrapper.get('[data-agent-rename-input]').setValue('  终端回归  ')
    await wrapper.get('[data-action="save-conversation-name"]').trigger('click')
    await flushPromises()

    expect(renameConversation).toHaveBeenCalledWith('conversation-1', '终端回归', expect.anything())
    expect(wrapper.get('[data-agent-conversation-title]').text()).toContain('终端回归')
    wrapper.unmount()
  })

  it('renders a nearby alert when the conversation directory cannot load', async () => {
    const listBindings = vi.fn(async () => { throw new Error('offline') })
    const { wrapper } = await mounted({ listBindings })

    expect(wrapper.get('[data-agent-error]').attributes('role')).toBe('alert')
    expect(wrapper.get('[data-agent-error]').text()).toContain('无法加载 Agent 会话数据')
    wrapper.unmount()
  })
})
