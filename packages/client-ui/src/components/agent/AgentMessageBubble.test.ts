import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AgentMessageState, AgentUserMessageState } from '@termflow/client-core'
import AgentMessageBubble from './AgentMessageBubble.vue'

function assistant(overrides: Partial<AgentMessageState> = {}): AgentMessageState {
  return { messageId: 'm1', role: 'assistant', text: 'hello', status: 'complete', createdAt: 0, ...overrides }
}

describe('AgentMessageBubble', () => {
  it('renders assistant and user bubbles with role classes', () => {
    const a = mount(AgentMessageBubble, { props: { message: assistant() } })
    expect(a.get('.agent-message').classes()).toContain('agent-message--assistant')
    expect(a.get('.agent-message').attributes('data-agent-message-role')).toBe('assistant')

    const user: AgentUserMessageState = { clientId: 'u1', text: 'hi', deliveryState: 'accepted', error: null }
    const u = mount(AgentMessageBubble, { props: { message: user } })
    expect(u.get('.agent-message').classes()).toContain('agent-message--user')
    expect(u.get('.agent-message').attributes('data-agent-message-role')).toBe('user')
    a.unmount()
    u.unmount()
  })

  it('renders HTML-shaped text literally — no element injection, no links', () => {
    const wrapper = mount(AgentMessageBubble, { props: { message: assistant({ text: '<script>alert(1)</script>' }) } })
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toBe('<script>alert(1)</script>')
    wrapper.unmount()
  })

  it('renders URL-shaped text and tags as inert text', () => {
    const wrapper = mount(AgentMessageBubble, { props: { message: assistant({ text: 'go <b>bold</b> at javascript:alert(1) now' }) } })
    expect(wrapper.find('b').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toBe('go <b>bold</b> at javascript:alert(1) now')
    wrapper.unmount()
  })

  it('strips ANSI/OSC sequences before rendering', () => {
    const wrapper = mount(AgentMessageBubble, {
      props: { message: assistant({ text: '\u001b[31m红色\u001b[0m \u001b]8;;javascript:alert(1)\u0007点击\u001b]8;;\u0007' }) },
    })
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toBe('红色 点击')
    expect(wrapper.html()).not.toContain('\u001b')
    wrapper.unmount()
  })

  it('shows an aria-hidden cursor only while the assistant message streams', async () => {
    const wrapper = mount(AgentMessageBubble, { props: { message: assistant({ status: 'streaming' }) } })
    const cursor = wrapper.get('.agent-message__cursor')
    expect(cursor.attributes('aria-hidden')).toBe('true')
    expect(cursor.text()).toBe('')
    await wrapper.setProps({ message: assistant({ status: 'complete' }) })
    expect(wrapper.find('.agent-message__cursor').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps pre-wrap rendering for multi-line text (CSS contract)', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    expect(css).toMatch(/\.agent-message__text\s*\{[^}]*white-space:\s*pre-wrap/s)
  })
})
