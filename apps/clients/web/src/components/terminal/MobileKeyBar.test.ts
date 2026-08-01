import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { MobileModifierController } from '../../terminal/modifiers'
import MobileKeyBar from './MobileKeyBar.vue'

describe('MobileKeyBar', () => {
  it('disables every terminal-input control while the stream is not ready', async () => {
    const wrapper = mount(MobileKeyBar, { props: { prefix: 'C-a', controller: new MobileModifierController(), disabled: true } })
    expect(wrapper.findAll('button')).toHaveLength(6)
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.findAll('button')[3].trigger('click')
    expect(wrapper.emitted('input')).toBeUndefined()
  })
})
