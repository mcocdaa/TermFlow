import { mount } from '@vue/test-utils'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { describe, expect, it } from 'vitest'
import { MobileModifierController } from '@termflow/client-core'
import MobileKeyBar from './MobileKeyBar.vue'

const mountKeyBar = (props: InstanceType<typeof MobileKeyBar>['$props']) => mount(MobileKeyBar, {
  props,
  global: { plugins: [createClientUi(createFakeRuntime())] },
})

describe('MobileKeyBar', () => {
  it('keeps the platform-neutral modifier state reactive in Vue', async () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController() })
    const ctrl = wrapper.findAll('button')[0]
    expect(ctrl!.attributes('aria-pressed')).toBe('false')
    await ctrl!.trigger('click')
    expect(ctrl!.attributes('aria-pressed')).toBe('true')
    await ctrl!.trigger('click')
    expect(ctrl!.find('.locked-indicator').exists()).toBe(true)
  })

  it('disables every terminal-input control while the stream is not ready', async () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController(), disabled: true })
    expect(wrapper.findAll('button')).toHaveLength(6)
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.findAll('button')[3]!.trigger('click')
    expect(wrapper.emitted('input')).toBeUndefined()
  })
})
