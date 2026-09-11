import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentBackendStatus from './AgentBackendStatus.vue'

it('shows a connecting status before the backend emits its first state', () => {
  const wrapper = mount(AgentBackendStatus, { props: { state: null } })

  expect(wrapper.text()).toBe('连接中')
  expect(wrapper.attributes('data-agent-backend-state')).toBe('unknown')
  expect(wrapper.classes()).toContain('agent-backend-status--connecting')
})

it('labels the live wire states instead of falling back to 未知', () => {
  for (const [state, label] of [
    ['idle', '空闲'],
    ['busy', '处理中'],
    ['retry', '重试中'],
    ['ready', '就绪'],
  ] as const) {
    const wrapper = mount(AgentBackendStatus, { props: { state } })
    expect(wrapper.text()).toBe(label)
    expect(wrapper.attributes('data-agent-backend-state')).toBe(state)
  }
})

it('hides the chip for an unknown wire drift value', () => {
  const wrapper = mount(AgentBackendStatus, { props: { state: 'something-new' } })

  expect(wrapper.find('[data-agent-backend-state]').exists()).toBe(false)
})
