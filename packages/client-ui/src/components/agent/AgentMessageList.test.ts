import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { ApiError, createAgentHistoryState, type AgentHistoryState } from '@termflow/client-core'
import { createClientUi, type ClientRuntime } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AgentMessageList from './AgentMessageList.vue'

/**
 * The list now hosts approval cards (M6b §4.7 timeline integration), which
 * lazily fetch the REST detail — the fake request rejects so the card's
 * 详情不可用 fallback path renders deterministically.
 */
function mountList(history: AgentHistoryState) {
  const runtime = createFakeRuntime({
    api: {
      ...createFakeRuntime().api,
      request: vi.fn(async () => {
        throw new ApiError('server', { status: 404 })
      }),
    } as unknown as ClientRuntime['api'],
  })
  return mount(AgentMessageList, {
    props: { history },
    global: { plugins: [createClientUi(runtime)] },
  })
}

function historyWith(
  messages: Array<{ id: string; text: string; status?: 'streaming' | 'complete' }> = [],
): AgentHistoryState {
  const state = createAgentHistoryState()
  for (const m of messages) {
    state.messages.set(m.id, { messageId: m.id, role: 'assistant', text: m.text, status: m.status ?? 'complete', createdAt: 0 })
    state.timeline.push({ type: 'message', refId: m.id, at: 0 })
  }
  return state
}

function withUser(state: AgentHistoryState, id: string, text: string): AgentHistoryState {
  state.userMessages.set(id, { clientId: id, text, deliveryState: 'accepted', error: null })
  state.timeline.push({ type: 'user', refId: id, at: 0 })
  return state
}

function withRunningTool(state: AgentHistoryState, id: string, name: string): AgentHistoryState {
  state.toolCalls.set(id, { toolCallId: id, toolName: name, status: 'running', summary: null, startedAt: 0, endedAt: null })
  state.timeline.push({ type: 'tool', refId: id, at: 0 })
  return state
}

/** Fake a scrollable viewport on a jsdom element (no layout engine). */
function mockScroll(el: HTMLElement, layout: { scrollHeight: number; clientHeight: number }) {
  let scrollTop = 0
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => layout.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => layout.clientHeight })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => {
      // Real browsers clamp assignments to the scrollable range.
      scrollTop = Math.max(0, Math.min(value, layout.scrollHeight - layout.clientHeight))
    },
  })
  return {
    get: () => scrollTop,
    set: (value: number) => {
      scrollTop = value
    },
  }
}

describe('AgentMessageList', () => {
  it('exposes the log live region with additions-only announcements', () => {
    const wrapper = mountList(historyWith([]))
    const log = wrapper.get('[data-agent-message-list]')
    expect(log.attributes('role')).toBe('log')
    expect(log.attributes('aria-live')).toBe('polite')
    expect(log.attributes('aria-relevant')).toBe('additions')
    wrapper.unmount()
  })

  it('keeps a visually hidden coarse-grained status region (never per chunk)', () => {
    const wrapper = mountList(historyWith([]))
    const status = wrapper.get('[data-agent-status]')
    expect(status.attributes('role')).toBe('status')
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.classes()).toContain('sr-only')
    expect(status.text()).toBe('')
    wrapper.unmount()
  })

  it('renders messages and user echoes in timeline order, skipping dangling refs', () => {
    const state = historyWith([{ id: 'a1', text: '第一条' }, { id: 'a2', text: '第二条' }])
    state.timeline.push({ type: 'message', refId: 'missing', at: 0 })
    withUser(state, 'u1', '用户消息')
    const wrapper = mountList(state)
    const bubbles = wrapper.findAll('.agent-message')
    expect(bubbles.length).toBe(3)
    expect(bubbles.map((b) => b.text())).toEqual(['第一条', '第二条', '用户消息'])
    expect(bubbles[2]!.classes()).toContain('agent-message--user')
    wrapper.unmount()
  })

  it('interleaves tool rows and approval cards into the flow in timeline order', async () => {
    const state = historyWith([{ id: 'a1', text: '开始' }])
    state.toolCalls.set('t1', { toolCallId: 't1', toolName: 'ls', status: 'completed', summary: '{"ok":true}', startedAt: 0, endedAt: 1 })
    state.timeline.push({ type: 'tool', refId: 't1', at: 0 })
    state.messages.set('a2', { messageId: 'a2', role: 'assistant', text: '结束', status: 'complete', createdAt: 0 })
    state.timeline.push({ type: 'message', refId: 'a2', at: 0 })
    state.permissions.set('p1', { approvalId: 'p1', toolName: 'rm', evidence: null, expiresAt: null, state: 'pending', decidedAt: null })
    state.timeline.push({ type: 'permission', refId: 'p1', at: 0 })

    const wrapper = mountList(state)
    await flushPromises()

    const rendered = [...wrapper.get('[data-agent-message-list]').element.children]
      .map((el) => (el.className as string).split(' ')[0])
    expect(rendered).toEqual(['agent-message', 'agent-tool-activity', 'agent-message', 'agent-approval-card'])
    expect(wrapper.get('[data-agent-tool-status-label]').text()).toBe('已完成')
    expect(wrapper.get('[data-agent-approval-card]').attributes('data-agent-approval-id')).toBe('p1')
    wrapper.unmount()
  })

  it('forwards the approval card focus request for the panel handoff', async () => {
    const state = historyWith([])
    state.permissions.set('p1', { approvalId: 'p1', toolName: 'rm', evidence: null, expiresAt: null, state: 'pending', decidedAt: null })
    state.timeline.push({ type: 'permission', refId: 'p1', at: 0 })
    const wrapper = mountList(state)
    await flushPromises()

    await wrapper.get('[data-action="focus-approval"]').trigger('click')
    expect(wrapper.emitted('focus-approval')).toEqual([['p1']])
    wrapper.unmount()
  })

  it('announces only new bubbles: streaming updates patch the existing node in place', async () => {
    const wrapper = mountList(historyWith([{ id: 'a1', text: '初始', status: 'streaming' }]))
    const textNode = wrapper.get('.agent-message__text').element

    await wrapper.setProps({ history: historyWith([{ id: 'a1', text: '初始增量', status: 'streaming' }]) })
    expect(wrapper.findAll('.agent-message').length).toBe(1)
    expect(wrapper.get('.agent-message__text').element).toBe(textNode)
    expect(wrapper.get('.agent-message__text').text()).toBe('初始增量')

    await wrapper.setProps({
      history: historyWith([{ id: 'a1', text: '初始增量', status: 'streaming' }, { id: 'a2', text: '新气泡' }]),
    })
    expect(wrapper.findAll('.agent-message').length).toBe(2)
    wrapper.unmount()
  })

  it('broadcasts a tool completion in the status region, then clears on chunk-only updates', async () => {
    const s1 = historyWith([{ id: 'a1', text: 'hello' }])
    withRunningTool(s1, 't1', 'ls')
    const wrapper = mountList(s1)
    expect(wrapper.get('[data-agent-status]').text()).toBe('')

    const s2 = historyWith([{ id: 'a1', text: 'hello' }])
    s2.toolCalls.set('t1', { toolCallId: 't1', toolName: 'ls', status: 'completed', summary: '{}', startedAt: 0, endedAt: 1 })
    s2.timeline.push({ type: 'tool', refId: 't1', at: 0 })
    await wrapper.setProps({ history: s2 })
    expect(wrapper.get('[data-agent-status]').text()).toContain('ls 已完成')

    // A streaming chunk carries no coarse event: the region is cleared and
    // nothing about the chunk text is announced.
    const s3 = historyWith([{ id: 'a1', text: 'hello + chunk', status: 'streaming' }])
    s3.toolCalls.set('t1', { toolCallId: 't1', toolName: 'ls', status: 'completed', summary: '{}', startedAt: 0, endedAt: 1 })
    s3.timeline.push({ type: 'tool', refId: 't1', at: 0 })
    await wrapper.setProps({ history: s3 })
    expect(wrapper.get('[data-agent-status]').text()).toBe('')
    wrapper.unmount()
  })

  it('announces approval arrivals and backend state changes once', async () => {
    const wrapper = mountList(historyWith([]))

    const s2 = historyWith([])
    s2.permissions.set('p1', { approvalId: 'p1', toolName: 'rm', evidence: null, expiresAt: null, state: 'pending', decidedAt: null })
    s2.timeline.push({ type: 'permission', refId: 'p1', at: 0 })
    s2.backend.state = 'ready'
    s2.backend.epoch = 1
    await wrapper.setProps({ history: s2 })
    const text = wrapper.get('[data-agent-status]').text()
    expect(text).toContain('收到新的审批请求')
    expect(text).toContain('后端状态：ready')

    // Unchanged permission and backend state stay silent.
    const s3 = historyWith([])
    s3.permissions.set('p1', { approvalId: 'p1', toolName: 'rm', evidence: null, expiresAt: null, state: 'pending', decidedAt: null })
    s3.timeline.push({ type: 'permission', refId: 'p1', at: 0 })
    s3.backend.state = 'ready'
    s3.backend.epoch = 1
    await wrapper.setProps({ history: s3 })
    expect(wrapper.get('[data-agent-status]').text()).toBe('')
    wrapper.unmount()
  })

  it('auto-scrolls only while the user stays pinned to the bottom', async () => {
    const wrapper = mountList(historyWith([{ id: 'a1', text: '一' }]))
    const el = wrapper.get('[data-agent-message-list]').element as HTMLElement
    const layout = { scrollHeight: 400, clientHeight: 100 }
    const scroll = mockScroll(el, layout)

    // User at the bottom → a new bubble is followed to the new bottom.
    scroll.set(300)
    await wrapper.get('[data-agent-message-list]').trigger('scroll')
    layout.scrollHeight = 500
    await wrapper.setProps({ history: historyWith([{ id: 'a1', text: '一' }, { id: 'a2', text: '二' }]) })
    await nextTick()
    expect(scroll.get()).toBe(400)

    // User scrolled up → position is kept.
    scroll.set(0)
    await wrapper.get('[data-agent-message-list]').trigger('scroll')
    layout.scrollHeight = 600
    await wrapper.setProps({ history: historyWith([{ id: 'a1', text: '一' }, { id: 'a2', text: '二' }, { id: 'a3', text: '三' }]) })
    await nextTick()
    expect(scroll.get()).toBe(0)

    // Back to the bottom → followed again.
    scroll.set(500)
    await wrapper.get('[data-agent-message-list]').trigger('scroll')
    layout.scrollHeight = 700
    await wrapper.setProps({
      history: historyWith([{ id: 'a1', text: '一' }, { id: 'a2', text: '二' }, { id: 'a3', text: '三' }, { id: 'a4', text: '四' }]),
    })
    await nextTick()
    expect(scroll.get()).toBe(600)
    wrapper.unmount()
  })

  it('disables smooth scrolling under prefers-reduced-motion (CSS contract)', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    expect(css).toMatch(/\.agent-message-list\s*\{[^}]*scroll-behavior:\s*smooth/s)
    const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[^}]*\}/g) ?? []
    const agentBlock = reduced.find((block) => block.includes('.agent-message-list'))
    expect(agentBlock).toBeDefined()
    expect(agentBlock).toMatch(/\.agent-message-list\s*\{[^}]*scroll-behavior:\s*auto/s)
  })
})
