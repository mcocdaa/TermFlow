import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TotpProtectionLabel from '../settings/TotpProtectionLabel.vue'

describe('ContextHelp', () => {
  it('connects one shared question-mark trigger to wrapping tooltip copy', () => {
    const wrapper = mount(TotpProtectionLabel)
    const help = wrapper.get('[data-context-help]')
    const trigger = help.get('button')
    const tooltip = help.get('[role="tooltip"]')

    expect(trigger.attributes('aria-label')).toBe('说明启用双重认证登录')
    expect(trigger.attributes('aria-describedby')).toBe(tooltip.attributes('id'))
    expect(tooltip.text()).toContain('一次性验证码')
    expect(help.find('svg').exists()).toBe(true)
  })
})
