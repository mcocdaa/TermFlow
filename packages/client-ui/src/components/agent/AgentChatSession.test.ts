import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { expect, it, vi } from 'vitest'
import { ApiError } from '@termflow/client-core'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AgentChatSession from './AgentChatSession.vue'

it('stops polling and reports a conversation that no longer exists', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  try {
    const runtime = createFakeRuntime()
    const parts = vi.fn(async () => { throw new ApiError('server', { status: 404, code: 'not_found' }) })
    runtime.api.agents.getConversationParts = parts as never
    runtime.api.agents.getConversation = async () => { throw new Error('unavailable') }
    runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
    runtime.api.request = async () => ({ approvals: [], events: [], next_cursor: null }) as never
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
    await router.push('/')
    const w = mount(AgentChatSession, { props: { conversationId: 'gone', enabled: true, variant: 'sidecar' }, global: { plugins: [router, createClientUi(runtime)] } })
    await flushPromises()
    expect(parts).toHaveBeenCalledTimes(1)
    expect(w.emitted('missing')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(6_000)
    expect(parts).toHaveBeenCalledTimes(1)
    w.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('renders one root and omits page navigation in sidecar mode', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getConversation = async () => { throw new Error('unavailable') }
  runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
  runtime.api.request = async () => ({ approvals: [], events: [], next_cursor: null }) as never
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/agent', component: { template: '<div />' } }] })
  await router.push('/')
  const wrapper = mount(AgentChatSession, { props: { conversationId: 'c1', enabled: true, variant: 'sidecar' }, global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  expect(wrapper.element.nodeType).toBe(Node.ELEMENT_NODE)
  expect(wrapper.find('[data-action="back-to-overview"]').exists()).toBe(false)
  expect(wrapper.find('section.agent-chat-session').exists()).toBe(true)
  wrapper.unmount()
})

it('uses a Lucide icon for the page back affordance', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getConversation = async () => { throw new Error('unavailable') }
  runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
  runtime.api.request = async () => ({ approvals: [], events: [], next_cursor: null }) as never
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/agent', component: { template: '<div />' } }] })
  await router.push('/')
  const wrapper = mount(AgentChatSession, { props: { conversationId: 'c1', enabled: true, variant: 'page' }, global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  expect(wrapper.find('[data-action="back-to-overview"] svg').exists()).toBe(true)
  wrapper.unmount()
})

it('opens the approval tray and focuses a requested approval locally', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getConversation = async () => { throw new Error('unavailable') }
  runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
  const approval = (id: string) => ({ approval_id: id, state: 'pending', pane_id: '%1', operation: 'read', intent_summary: 'Read', canonical_hash: 'abcdef', expires_at: '2030-01-01' })
  runtime.api.request = async (path) => (path.includes('/approvals') ? { approvals: [approval('a1'), approval('a2')] } : { events: [], next_cursor: null }) as never
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
  await router.push('/')
  const w = mount(AgentChatSession, { attachTo: document.body, props: { conversationId: 'c1', enabled: true, variant: 'sidecar' }, global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  w.vm.focusApproval('a2'); await flushPromises()
  expect(w.find('[data-agent-approval-prompt]').exists()).toBe(true)
  expect(document.activeElement).toBe(w.get('[data-agent-approval-id="a2"] [data-action="approve-approval"]').element)
  expect(w.emitted('pendingCount')?.at(-1)).toEqual([2])
  w.unmount()
})

it('keeps a deferred approval focus request until the delayed REST list renders', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.getConversation = async () => { throw new Error('unavailable') }
  runtime.api.agents.listMessages = async () => ({ messages: [] }) as never
  const approval = { approval_id: 'late-approval', state: 'pending', pane_id: '%1', operation: 'read', intent_summary: 'Read', canonical_hash: 'abcdef', expires_at: '2030-01-01' }
  let resolveApprovals!: (value: unknown) => void
  const approvalsReady = new Promise((resolve) => { resolveApprovals = resolve })
  runtime.api.request = vi.fn(async (path) => path.includes('/approvals')
    ? await approvalsReady
    : { events: [], next_cursor: null }) as never
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
  await router.push('/')
  const w = mount(AgentChatSession, { attachTo: document.body, props: { conversationId: 'c1', enabled: true, variant: 'sidecar' }, global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  w.vm.focusApproval('late-approval')
  await flushPromises()
  expect(document.activeElement?.getAttribute('data-action')).not.toBe('approve-approval')

  resolveApprovals({ approvals: [approval] })
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(w.find('[data-agent-approval-prompt]').exists()).toBe(true)
  expect(document.activeElement).toBe(w.get('[data-agent-approval-id="late-approval"] [data-action="approve-approval"]').element)
  w.unmount()
})
