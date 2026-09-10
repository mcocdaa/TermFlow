import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentBackendStatus from './AgentBackendStatus.vue'

it('shows a connecting status before the backend emits its first state', () => {
  const wrapper = mount(AgentBackendStatus, { props: { state: null } })

  expect(wrapper.text()).toBe('连接中')
  expect(wrapper.attributes('data-agent-backend-state')).toBe('unknown')
  expect(wrapper.classes()).toContain('agent-backend-status--connecting')
})
