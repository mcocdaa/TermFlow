import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import ThemePicker from './ThemePicker.vue'
import { activeTheme, THEME_STORAGE_KEY } from '../../stores/theme'

describe('ThemePicker', () => {
  beforeEach(() => { activeTheme.value = 'graphite-signal' })
  it('shows three named radio options and persists only the selected identifier', async () => {
    const wrapper = mount(ThemePicker)
    const radios = wrapper.findAll('[role="radio"]')
    expect(radios.map((radio) => radio.text())).toEqual(['石墨信号', '云端钴蓝', '午夜靛蓝'])

    await radios[1].trigger('click')

    expect(radios[1].attributes('aria-checked')).toBe('true')
    expect(document.documentElement.dataset.theme).toBe('cloud-cobalt')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('cloud-cobalt')
    expect(localStorage.length).toBe(1)
  })

  it('supports arrow-key selection', async () => {
    const wrapper = mount(ThemePicker)
    await wrapper.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.findAll('[role="radio"]')[1].attributes('aria-checked')).toBe('true')
  })
})
