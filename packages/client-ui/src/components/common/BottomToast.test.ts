import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import BottomToast from './BottomToast.vue'

describe('BottomToast', () => {
  it('renders a dismissing bottom status toast with success tone', () => {
    const wrapper = mount(BottomToast, { props: { message: '已授权', tone: 'success' } })

    expect(wrapper.get('[data-bottom-toast]').text()).toBe('已授权')
    expect(wrapper.get('[data-bottom-toast]').attributes('role')).toBe('status')
    expect(wrapper.get('[data-bottom-toast]').attributes('data-tone')).toBe('success')
  })
})
