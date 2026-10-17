import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import { createBottomToast } from '../../composables/useBottomToast'
import { setLocale, t } from '../../i18n'
import { bottomToastKey } from '../../runtimeKey'
import BottomToast from './BottomToast.vue'

describe('BottomToast', () => {
  it('renders a dismissing bottom status toast with success tone', () => {
    const wrapper = mount(BottomToast, { props: { message: '已授权', tone: 'success' } })

    expect(wrapper.get('[data-bottom-toast]').text()).toBe('已授权')
    expect(wrapper.get('[data-bottom-toast]').attributes('role')).toBe('status')
    expect(wrapper.get('[data-bottom-toast]').attributes('data-tone')).toBe('success')
  })

  it('reactively translates visible bottom toast when locale toggles between zh and en', async () => {
    setLocale('zh')
    const controller = createBottomToast({
      now: () => 0,
      setTimeout: () => 1,
      clearTimeout: () => {},
      setInterval: () => 1,
      clearInterval: () => {},
    })
    controller.show({ text: () => t('agent.chat.forbiddenToast'), tone: 'error' })

    const wrapper = mount(BottomToast, {
      global: {
        provide: {
          [bottomToastKey as symbol]: controller,
        },
      },
    })

    expect(wrapper.get('[data-bottom-toast]').text()).toBe('当前客户端没有访问该会话的权限，请重新登录授权。')
    setLocale('en')
    await nextTick()
    try {
      expect(wrapper.get('[data-bottom-toast]').text()).toBe('This client does not have permission to access this session. Please sign in and authorize again.')
    } finally {
      setLocale('zh')
      await nextTick()
    }
    expect(wrapper.get('[data-bottom-toast]').text()).toBe('当前客户端没有访问该会话的权限，请重新登录授权。')
  })
})
