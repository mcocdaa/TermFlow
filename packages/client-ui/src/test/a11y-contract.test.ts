import { flushPromises, mount } from '@vue/test-utils'
import { createAgentHistoryState, type AgentStreamConnectRequest, type AgentStreamTransport, type AgentStreamTransportEvent, type AguiEvent } from '@termflow/client-core'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import AgentApprovalPanel from '../components/agent/AgentApprovalPanel.vue'
import AgentMessageList from '../components/agent/AgentMessageList.vue'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'
import StatusPill from '../components/dashboard/StatusPill.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import AgentChatView from '../views/AgentChatView.vue'
import { clientRoutes } from '../router/routes'
import { createClientUi, type ClientRuntime } from '../runtime'
import { createFakeRuntime } from './fakeRuntime'

describe('accessibility contracts', () => {
  it('provides skip navigation, named navigation landmarks, and a focusable main target', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router, createClientUi(createFakeRuntime())] } })
    expect(wrapper.get('[href="#main-content"]')).toBeTruthy()
    expect(wrapper.get('aside[aria-label="主导航"]')).toBeTruthy()
    expect(wrapper.get('nav[aria-label="移动端导航"]')).toBeTruthy()
    expect(wrapper.get('main').attributes('tabindex')).toBe('-1')
    wrapper.unmount()
  })

  it('traps modal focus and restores it to the invoking control', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%1', paneName: 'Shell' } })
    await wrapper.vm.$nextTick()
    expect(document.activeElement?.textContent).toContain('取消')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement?.textContent).toContain('取消')
    wrapper.unmount()
    expect(document.activeElement).toBe(invoker)
    invoker.remove()
  })

  it('closes the titlebar tmux menu with Escape and restores trigger focus', async () => {
    const wrapper = mount(TmuxActionMenu, { attachTo: document.body, props: { bindings: { prefix: 'C-a', bindings: [] }, activePaneId: '%1' } })
    const trigger = wrapper.get('[data-action="toggle-tmux-menu"]')
    expect(trigger.attributes('aria-label')).toBe('tmux 操作')
    await trigger.trigger('click')
    await wrapper.setProps({ open: true })
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    await wrapper.setProps({ open: false })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('uses visible focus, reduced-motion guards, and status text independent of color', () => {
    const css = `${readFileSync(resolve(process.cwd(), 'src/styles/reset.css'), 'utf8')}\n${readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')}`
    expect(css).toContain(':focus-visible')
    expect(css).toContain('prefers-reduced-motion: no-preference')
    const online = mount(StatusPill, { props: { online: true } })
    expect(online.text()).toContain('在线')
    expect(online.get('[aria-hidden="true"]').text()).toBe('')
    const offline = mount(StatusPill, { props: { online: false } })
    expect(offline.text()).toContain('离线')
    online.unmount()
    offline.unmount()
  })
})

// ---------------------------------------------------------------------------
// M6b Agent chat a11y additions (spec §4.7/§8): the message flow is an
// additions-only log region with a coarse status announcer, approval actions
// are native keyboard-reachable buttons in stable tab order, approve goes
// through the ClosePaneDialog focus-trap pattern, scroll/cursor animations
// honour prefers-reduced-motion, and streaming updates never steal focus
// from the composer.
// ---------------------------------------------------------------------------

/** Extract the media-query block (balanced braces) containing a marker rule. */
function mediaRuleBlock(css: string, mediaQuery: string, marker: string): string {
  let searchFrom = 0
  for (;;) {
    const start = css.indexOf(mediaQuery, searchFrom)
    if (start < 0) return ''
    const open = css.indexOf('{', start)
    let depth = 0
    for (let index = open; index < css.length; index += 1) {
      if (css[index] === '{') depth += 1
      else if (css[index] === '}') {
        depth -= 1
        if (depth === 0) {
          const block = css.slice(open + 1, index)
          if (block.includes(marker)) return block
          searchFrom = index + 1
          break
        }
      }
    }
  }
}

function approvalFixture(): ApprovalResponse {
  return {
    approval_id: 'approval-1',
    binding_id: 'binding-1',
    conversation_id: 'conv-1',
    run_id: null,
    tool_call_id: 'tool-1',
    canonical_hash: 'abc123def456',
    state: 'pending',
    expires_at: '1970-01-01T00:05:00.000Z',
    decided_at: null,
    decision: null,
    auth_epoch: 7,
    created_at: '1970-01-01T00:00:00.000Z',
    pane_id: 'main:0.1',
    operation: 'write',
    intent_summary: '写入 /etc/hosts',
  }
}

function mountApprovalPanel() {
  const request = vi.fn(async () => ({ approvals: [approvalFixture()] }))
  const runtime = createFakeRuntime({
    api: { ...createFakeRuntime().api, request } as unknown as ClientRuntime['api'],
  })
  const wrapper = mount(AgentApprovalPanel, {
    props: { conversationId: 'conv-1' },
    global: { plugins: [createClientUi(runtime)] },
    attachTo: document.body,
  })
  return wrapper
}

/** Scripted agui transport: records connect and replays frames on demand. */
class ScriptedAgentTransport implements AgentStreamTransport<AguiEvent> {
  readonly emitters: Array<(event: AgentStreamTransportEvent<AguiEvent>) => void> = []
  async connect(_request: AgentStreamConnectRequest, emit: (event: AgentStreamTransportEvent<AguiEvent>) => void) {
    this.emitters.push(emit)
    return { close: async () => undefined }
  }
  emit(event: AgentStreamTransportEvent<AguiEvent>) {
    this.emitters.at(-1)?.(event)
  }
}

async function mountChatView() {
  const transport = new ScriptedAgentTransport()
  const request = vi.fn(async (path: unknown) => {
    const url = String(path)
    if (url.includes('/events')) return { events: [], next_cursor: null }
    if (url.startsWith('/api/v1/agent/approvals')) return { approvals: [] }
    return {}
  })
  const runtime = createFakeRuntime({
    createAgentStream: () => transport,
    api: {
      ...createFakeRuntime().api,
      agents: {
        capabilities: vi.fn(async () => ({
          agent_broker_enabled: true,
          delegated_write_grants_enabled: false,
          speech_to_text_enabled: false,
        })),
        listMessages: vi.fn(async () => ({ messages: [] })),
        submitMessage: vi.fn(async () => ({})),
        cancelRun: vi.fn(async () => ({})),
        getConversation: vi.fn(async () => ({})),
      },
      request,
    } as unknown as ClientRuntime['api'],
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agent', component: { template: '<div />' } },
      { path: '/agent/:conversationId', component: AgentChatView },
      { path: '/login', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  await router.push('/agent/conv-1')
  await router.isReady()
  const wrapper = mount(AgentChatView, {
    attachTo: document.body,
    global: { plugins: [router, createClientUi(runtime)] },
  })
  await flushPromises()
  return { wrapper, transport }
}

describe('agent chat accessibility contracts', () => {
  it('keeps the message flow an additions-only log with a visually hidden coarse status announcer', () => {
    const wrapper = mount(AgentMessageList, {
      props: { history: createAgentHistoryState() },
      global: { plugins: [createClientUi(createFakeRuntime())] },
    })
    const log = wrapper.get('[data-agent-message-list]')
    expect(log.attributes('role')).toBe('log')
    expect(log.attributes('aria-live')).toBe('polite')
    expect(log.attributes('aria-relevant')).toBe('additions')
    const status = wrapper.get('[data-agent-status]')
    expect(status.attributes('role')).toBe('status')
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.classes()).toContain('sr-only')
    wrapper.unmount()
  })

  it('approval actions are native keyboard-reachable buttons in approve → deny → revoke tab order', async () => {
    const wrapper = mountApprovalPanel()
    await flushPromises()

    const actions = wrapper.get('[data-agent-approval-item]').findAll('button')
    // Tab order = DOM order: approve, then deny, then revoke (spec §4.7).
    expect(actions.map((button) => button.attributes('data-action'))).toEqual([
      'approve-approval',
      'deny-approval',
      'revoke-approval',
    ])
    for (const button of actions) {
      expect(button.element.tagName).toBe('BUTTON')
      expect(button.attributes('type')).toBe('button')
      expect(button.attributes('disabled')).toBeUndefined()
      // No negative tabindex: every action stays in the natural tab order.
      expect(Number(button.element.getAttribute('tabindex') ?? 0)).toBeGreaterThanOrEqual(0)
    }
    wrapper.unmount()
  })

  it('approve confirmation traps focus and Escape restores the approving button', async () => {
    const wrapper = mountApprovalPanel()
    await flushPromises()

    const approveButton = wrapper.get('[data-action="approve-approval"]')
    // jsdom never focuses on a dispatched click: focus explicitly so the
    // dialog captures the real trigger for focus restoration.
    ;(approveButton.element as HTMLButtonElement).focus()
    await approveButton.trigger('click')
    await nextTick()

    const dialog = wrapper.get('[data-agent-approval-dialog]')
    const cancelButton = wrapper.get('[data-action="approve-cancel"]')
    const confirmButton = wrapper.get('[data-action="approve-confirm"]')
    // Initial focus lands on the cancel button (ClosePaneDialog pattern).
    expect(document.activeElement).toBe(cancelButton.element)

    // Shift+Tab from the first focusable wraps to the last.
    await dialog.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(confirmButton.element)

    // Escape cancels the dialog and hands focus back to the approve button.
    await dialog.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[data-agent-approval-dialog]').exists()).toBe(false)
    expect(document.activeElement).toBe(approveButton.element)
    wrapper.unmount()
  })

  it('disables message-list smooth scrolling and the streaming cursor animation under prefers-reduced-motion', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    // The default experience animates: smooth scroll and a blinking cursor.
    expect(css).toMatch(/\.agent-message-list\s*\{[^}]*scroll-behavior:\s*smooth/s)
    // Extract the whole reduced-motion block with balanced braces (the block
    // holds two rules, so a flat regex would stop at the first inner `}`).
    const block = mediaRuleBlock(css, '@media (prefers-reduced-motion: reduce)', '.agent-message-list')
    expect(block).toMatch(/\.agent-message-list\s*\{[^}]*scroll-behavior:\s*auto/s)
    expect(block).toMatch(/\.agent-message__cursor\s*\{[^}]*animation:\s*none/s)
  })

  it('streaming updates never move focus away from the composer', async () => {
    const { wrapper, transport } = await mountChatView()
    const composer = wrapper.get('[data-agent-composer-input]')
    ;(composer.element as HTMLTextAreaElement).focus()
    expect(document.activeElement).toBe(composer.element)

    // A full stream tail: chunks, tool activity, message end, backend delta.
    transport.emit({ type: 'open' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_CHUNK', messageId: 'm1', delta: '助手回答' }, cursor: '7-1' })
    transport.emit({ type: 'event', event: { type: 'TOOL_CALL_START', toolCallId: 't1', toolCallName: 'ls' }, cursor: '7-2' })
    transport.emit({ type: 'event', event: { type: 'TOOL_CALL_RESULT', messageId: 't1', toolCallId: 't1', content: '{}' }, cursor: '7-3' })
    transport.emit({ type: 'event', event: { type: 'TEXT_MESSAGE_END', messageId: 'm1' }, cursor: '7-4' })
    transport.emit({ type: 'event', event: { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/backend', value: { state: 'ready', epoch: 1 } }] }, cursor: '7-5' })
    await flushPromises()

    expect(wrapper.findAll('.agent-message')).toHaveLength(1)
    expect(document.activeElement).toBe(composer.element)
    wrapper.unmount()
  })
})
