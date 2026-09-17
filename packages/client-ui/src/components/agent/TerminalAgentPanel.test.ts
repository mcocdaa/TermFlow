import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import TerminalAgentPanel from './TerminalAgentPanel.vue'
import { defineComponent, onUnmounted } from 'vue'
const staleDisclosure = {
  binding_id: 'b1',
  disclosure_fingerprint: 'current-fingerprint',
  provider_id: 'deepseek',
  model_id: 'deepseek-v4-flash',
  endpoint_origin: 'https://api.deepseek.com',
  region: 'global',
  retention_terms: 'retention terms',
  retention_version: 'retention-v1',
  no_training: true,
  policy_version: 'policy-v1',
  credential_source: 'DEEPSEEK_API_KEY',
  accepted: false,
  accepted_at: null,
  accepted_auth_epoch: null,
}
it.each(['loading', 'unconfigured', 'deployment_required', 'activating', 'unavailable'])('renders only the %s state', async (state) => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = (state === 'loading' ? vi.fn(() => new Promise(() => {})) : vi.fn(async () => ({ state, term_id: 't1', binding_id: null, profiles: [], disclosure: null }))) as never
  const w = mount(TerminalAgentPanel, { props: { termId: 't1', conversationId: null }, global: { plugins: [createClientUi(runtime)] } })
  await flushPromises()
  expect(w.findAll('[data-agent-panel-state]')).toHaveLength(1)
  expect(w.get('[data-agent-panel-state]').attributes('data-agent-panel-state')).toBe(state)
  expect(w.find('.agent-chat-session').exists()).toBe(false)
  if (state !== 'loading') expect(w.emitted('readiness')?.at(-1)).toEqual([state])
  w.unmount()
})

it('unmounts the previous chat once and forwards primitive pending counts', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [{ conversation_id: 'c1', binding_id: 'b1' }, { conversation_id: 'c2', binding_id: 'b1' }] })) as never
  const unmount = vi.fn()
  const Chat = defineComponent({ props: ['conversationId'], emits: ['pendingCount'], setup() { onUnmounted(unmount); return {} }, template: '<button data-chat @click="$emit(\'pendingCount\', 3)">{{ conversationId }}</button>' })
  const w = mount(TerminalAgentPanel, { props: { termId: 't1', conversationId: 'c1' }, global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: Chat } } })
  await flushPromises()
  await w.setProps({ conversationId: 'c2' }); await flushPromises()
  expect(w.get('[data-agent-panel-readiness]').text()).toBe('已就绪')
  expect(w.get('[data-chat]').text()).toBe('c2')
  expect(unmount).toHaveBeenCalledOnce()
  await w.get('[data-chat]').trigger('click')
  expect(w.emitted('pendingCount')?.at(-1)).toEqual([3])
  expect(w.emitted('readiness')?.at(-1)).toEqual(['ready'])
  w.unmount()
})

it('uses an icon toolbar and exposes edge resize handles for the floating panel', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [{ conversation_id: 'c1', binding_id: 'b1', title: '一个很长的会话标题', status: 'open', created_at: '', updated_at: '' }] })) as never
  const w = mount(TerminalAgentPanel, {
    props: { termId: 't1', conversationId: 'c1' },
    global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } },
  })
  await flushPromises()
  expect(w.find('[data-agent-floating-panel]').exists()).toBe(true)
  expect(w.find('[data-action="drag-agent"] svg').exists()).toBe(true)
  expect(w.find('[data-action="new-agent-conversation"] svg').exists()).toBe(true)
  expect(w.find('[data-action="toggle-agent-history"] svg').exists()).toBe(true)
  expect(w.get('[data-agent-panel-title]').text()).toContain('一个很长的会话标题')
  expect(w.findAll('[data-agent-resize-handle]')).toHaveLength(8)
  w.unmount()
})

it('keeps readiness and write policy in a compact status bar below the title', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [] })) as never
  const w = mount(TerminalAgentPanel, {
    props: { termId: 't1', conversationId: null },
    global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } },
  })
  await flushPromises()
  expect(w.find('[data-agent-panel-statusbar]').exists()).toBe(true)
  expect(w.get('[data-agent-panel-statusbar]').get('[data-agent-panel-readiness]').text()).toBe('已就绪')
  expect(w.find('[data-agent-panel-statusbar]').find('[data-agent-write-policy]').exists()).toBe(true)
  expect(w.get('.terminal-agent-panel__header').find('[data-agent-panel-readiness]').exists()).toBe(false)
  expect(w.get('.terminal-agent-panel__header').find('[data-agent-write-policy]').exists()).toBe(false)
  w.unmount()
})

it('uses the whole workspace on mobile without floating-window controls', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [{ conversation_id: 'c1', binding_id: 'b1', title: '手机会话', status: 'open', created_at: '', updated_at: '' }] })) as never
  const w = mount(TerminalAgentPanel, {
    props: { termId: 't1', conversationId: 'c1', mobilePage: true },
    global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } },
  })
  await flushPromises()
  expect(w.get('#terminal-agent-panel').attributes('data-agent-layout')).toBe('page')
  expect(w.find('[data-action="drag-agent"]').exists()).toBe(false)
  expect(w.findAll('[data-agent-resize-handle]')).toHaveLength(0)
  expect(w.find('[data-action="new-agent-conversation"] svg').exists()).toBe(true)
  expect(w.find('[data-action="toggle-agent-history"] svg').exists()).toBe(true)
  w.unmount()
})

it.each(['binding_disclosure_stale', 'binding_disclosure_required'])('re-enables the binding for %s without any disclosure card', async (reasonCode) => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({
    state: 'unavailable',
    term_id: 't1',
    binding_id: 'b1',
    profiles: [],
    disclosure: staleDisclosure,
    reason_code: reasonCode,
  })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [] })) as never
  runtime.api.agents.acceptDisclosure = vi.fn(async () => ({ ...staleDisclosure, accepted: true })) as never
  const w = mount(TerminalAgentPanel, { props: { termId: 't1', conversationId: null }, global: { plugins: [createClientUi(runtime)] } })
  await flushPromises()

  const recovery = w.get('[data-agent-disclosure-recovery]')
  expect(recovery.text()).toContain('请重新启用 Agent')
  for (const value of ['deepseek', 'https://api.deepseek.com', 'retention terms', 'DEEPSEEK_API_KEY', 'current-fingerprint']) {
    expect(recovery.text()).not.toContain(value)
  }
  await w.get('[data-action="accept-current-disclosure"]').trigger('click')
  await flushPromises()
  expect(runtime.api.agents.acceptDisclosure).toHaveBeenCalledWith(
    'b1',
    { disclosure_fingerprint: 'current-fingerprint', accepted: true },
    expect.any(AbortSignal),
  )
  w.unmount()
})

it('lets the user adjust pane grants after setup through the settings toolbar', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({
    state: 'ready',
    term_id: 't1',
    binding_id: 'b1',
    profiles: [],
    disclosure: null,
    topology_revision: 7,
    pane_policy: { binding_id: 'b1', pane_ids: ['%0'], topology_revision: 7 },
    runtime: { config_revision: 3 },
  })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [] })) as never
  runtime.api.terms.topology = vi.fn(async () => ({
    instance_id: 't1',
    topology: {
      session_id: '$0',
      session_name: 'demo',
      revision: 7,
      windows: [{
        window_id: '@0',
        panes: [
          { pane_id: '%0', window_id: '@0', title: 'bash', current_command: 'bash' },
          { pane_id: '%1', window_id: '@0', title: 'node', current_command: 'node' },
        ],
      }],
    },
  })) as never
  runtime.api.agents.replacePanePolicies = vi.fn(async () => ({ binding_id: 'b1', pane_ids: ['%0', '%1'], topology_revision: 7 })) as never
  const w = mount(TerminalAgentPanel, { props: { termId: 't1', conversationId: null }, global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } } })
  await flushPromises()

  expect(w.find('[data-agent-panel-settings]').exists()).toBe(false)
  await w.get('[data-action="toggle-agent-settings"]').trigger('click')
  expect(w.get('[data-agent-panel-settings]').text()).toContain('已选 1/2')
  await w.get('input[name="paneIds"][value="%1"]').setValue(true)
  expect(w.get('[data-agent-panel-settings]').text()).toContain('已选 2/2')
  await w.get('[data-action="save-agent-settings"]').trigger('click')
  await flushPromises()

  expect(runtime.api.agents.replacePanePolicies).toHaveBeenCalledWith('b1', {
    pane_ids: ['%0', '%1'],
    topology_revision: 7,
    expected_revision: 3,
  })
  expect(w.find('[data-agent-panel-settings]').exists()).toBe(false)
  w.unmount()
})

it('defers focus and URL selection until a persistently mounted sidecar opens', async () => {
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [{ conversation_id: 'c1', binding_id: 'b1' }] })) as never
  const w = mount(TerminalAgentPanel, {
    attachTo: document.body,
    props: { termId: 't1', conversationId: null, open: false },
    global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } },
  })
  await flushPromises()
  expect(document.activeElement).not.toBe(w.get('[data-action="close-agent"]').element)
  expect(w.emitted('selectConversation')).toBeUndefined()

  await w.setProps({ open: true })
  await flushPromises()
  expect(document.activeElement).toBe(w.get('[data-action="close-agent"]').element)
  expect(w.emitted('selectConversation')?.at(-1)).toEqual(['c1'])
  w.unmount()
})

it('does not clear an initial deep link while its requested conversation is loading', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getSetup = vi.fn(async () => ({ state: 'ready', term_id: 't1', binding_id: 'b1', profiles: [], disclosure: null })) as never
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [
    { conversation_id: 'c1', binding_id: 'b1' },
    { conversation_id: 'c2', binding_id: 'b1' },
  ] })) as never
  const w = mount(TerminalAgentPanel, {
    props: { termId: 't1', conversationId: 'c2', open: true },
    global: { plugins: [createClientUi(runtime)], stubs: { AgentChatSession: true } },
  })
  await flushPromises()
  expect(w.emitted('selectConversation')).not.toContainEqual([null])
  expect(w.emitted('selectConversation')?.at(-1)).toEqual(['c2'])
  w.unmount()
})
