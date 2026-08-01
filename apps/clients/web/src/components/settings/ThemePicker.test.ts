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
    const wrapper = mount(ThemePicker, { attachTo: document.body })
    const radios = wrapper.findAll('[role="radio"]')
    ;(radios[0].element as HTMLButtonElement).focus()
    await wrapper.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowRight' })
    expect(radios[1].attributes('aria-checked')).toBe('true')
    expect(radios[1].attributes('aria-label')).toBe('云端钴蓝')
    expect(document.activeElement).toBe(radios[1].element)
    wrapper.unmount()
  })
})
