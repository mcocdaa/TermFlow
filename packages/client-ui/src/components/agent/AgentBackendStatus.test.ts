import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentBackendStatus from './AgentBackendStatus.vue'

describe('AgentBackendStatus', () => {
  const cases: Array<[string, string]> = [
    ['connecting', '连接中'],
    ['ready', '就绪'],
    ['unavailable', '不可用'],
    ['context_lost', '上下文丢失'],
    ['reconciling', '恢复中'],
    ['closed', '已关闭'],
  ]

  it.each(cases)('maps the %s runtime state to "%s"', (state, label) => {
    const wrapper = mount(AgentBackendStatus, { props: { state } })
    expect(wrapper.get('[data-agent-backend-state]').attributes('data-agent-backend-state')).toBe(state)
    expect(wrapper.text()).toBe(label)
    wrapper.unmount()
  })

  it('labels unknown wire values and null as 未知', () => {
    for (const state of ['some_future_state', null] as const) {
      const wrapper = mount(AgentBackendStatus, { props: { state } })
      expect(wrapper.classes()).toContain('agent-backend-status--unknown')
      expect(wrapper.text()).toBe('未知')
      wrapper.unmount()
    }
  })

  it('keeps an unknown raw wire value verbatim in the data attribute (no data hiding)', () => {
    const wrapper = mount(AgentBackendStatus, { props: { state: 'some_future_state' } })
    // The class collapses to --unknown, but the raw value is preserved for debugging.
    expect(wrapper.classes()).toContain('agent-backend-status--unknown')
    expect(wrapper.attributes('data-agent-backend-state')).toBe('some_future_state')
    wrapper.unmount()
  })
})
