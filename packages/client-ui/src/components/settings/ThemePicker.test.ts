import { mount } from '@vue/test-utils'
import type { ThemeId } from '@termflow/design-tokens'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { createThemeState, type ThemePreferences, type ThemeState, type ThemeTarget } from '../../theme/theme'
import ThemePicker from './ThemePicker.vue'

let preferences: ThemePreferences
let target: ThemeTarget
let theme: ThemeState

describe('ThemePicker', () => {
  beforeEach(() => {
    preferences = { load: vi.fn<() => ThemeId | null>(() => 'graphite-signal'), save: vi.fn() }
    target = { apply: vi.fn((theme: ThemeId) => { document.documentElement.dataset.theme = theme }) }
    theme = createThemeState(preferences, target)
  })

  const mountPicker = () => mount(ThemePicker, {
    global: { plugins: [createClientUi(createFakeRuntime(), { theme })] },
  })

  it('shows three named radio options and persists only the selected identifier through the theme port', async () => {
    const wrapper = mountPicker()
    const radios = wrapper.findAll('[role="radio"]')
    expect(radios.map((radio) => radio.text())).toEqual(['石墨信号', '云端钴蓝', '午夜靛蓝'])

    await radios[1]!.trigger('click')

    expect(radios[1]!.attributes('aria-checked')).toBe('true')
    expect(theme.active.value).toBe('cloud-cobalt')
    expect(preferences.save).toHaveBeenCalledWith('cloud-cobalt')
    expect(target.apply).toHaveBeenLastCalledWith('cloud-cobalt')
  })

  it('supports arrow-key selection', async () => {
    const wrapper = mount(ThemePicker, {
      attachTo: document.body,
      global: { plugins: [createClientUi(createFakeRuntime(), { theme })] },
    })
    const radios = wrapper.findAll('[role="radio"]')
    ;(radios[0]!.element as HTMLButtonElement).focus()
    await wrapper.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowRight' })
    expect(radios[1]!.attributes('aria-checked')).toBe('true')
    expect(radios[1]!.attributes('aria-label')).toBe('云端钴蓝')
    expect(document.activeElement).toBe(radios[1]!.element)
    wrapper.unmount()
  })
})
