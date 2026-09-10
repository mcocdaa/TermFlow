import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentMessageBubble from './AgentMessageBubble.vue'

it('labels user and assistant messages and collapses system context by default', () => {
  const user = mount(AgentMessageBubble, {
    props: { message: { clientId: 'u1', text: '你好', deliveryState: 'accepted', error: null } },
  })
  expect(user.get('[data-agent-message-role-label]').text()).toBe('你')
  expect(user.get('[data-agent-message-role]').attributes('data-agent-message-role')).toBe('user')
  user.unmount()

  const system = mount(AgentMessageBubble, {
    props: { message: { messageId: 's1', role: 'system', text: 'system prompt', status: 'complete', createdAt: 1 } },
  })
  expect(system.get('[data-agent-message-role-label]').text()).toBe('系统')
  expect(system.get('details').attributes('open')).toBeUndefined()
  expect(system.get('summary').text()).toContain('系统上下文')
  system.unmount()
})
