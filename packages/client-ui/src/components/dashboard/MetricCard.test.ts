import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MetricCard from './MetricCard.vue'

describe('MetricCard', () => {
  it('exposes optional help on hover and keyboard focus without changing the value', () => {
    const wrapper = mount(MetricCard, {
      props: { label: '在线 Terms', value: 2, help: '当前在线并可远程控制的 Term，共 3 个 Term。' },
    })

    const card = wrapper.get('.metric-card')
    const tooltip = wrapper.get('[role="tooltip"]')
    expect(card.attributes('tabindex')).toBe('0')
    expect(card.attributes('aria-describedby')).toBe(tooltip.attributes('id'))
    expect(card.classes()).toContain('metric-card-has-help')
    expect(tooltip.text()).toContain('共 3 个 Term')
    expect(card.get('strong').text()).toBe('2')
  })

  it('does not create a fake focus target when no help is available', () => {
    const wrapper = mount(MetricCard, { props: { label: 'Computers', value: 1 } })
    expect(wrapper.get('.metric-card').attributes('tabindex')).toBeUndefined()
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false)
  })
})
