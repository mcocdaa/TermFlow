import { describe, expect, it, vi } from 'vitest'
import { LOCALE_STORAGE_KEY, setLocale, THEME_STORAGE_KEY } from '@termflow/client-ui'
import { createBrowserThemePreferences, createBrowserThemeTarget } from './browserThemePreferences'

describe('browser theme adapters', () => {
  it('persists only a validated theme identifier and wires locale preferences', () => {
    const storage = { getItem: vi.fn(() => 'cloud-cobalt'), setItem: vi.fn() }
    const preferences = createBrowserThemePreferences(storage)
    expect(preferences.load()).toBe('cloud-cobalt')
    preferences.save('midnight-indigo')
    expect(storage.setItem).toHaveBeenCalledWith(THEME_STORAGE_KEY, 'midnight-indigo')
    setLocale('en')
    expect(storage.setItem).toHaveBeenCalledWith(LOCALE_STORAGE_KEY, 'en')
    setLocale('zh')
  })

  it('rejects unknown stored values and applies through the injected root element', () => {
    const preferences = createBrowserThemePreferences({ getItem: () => 'admin-token-secret', setItem: vi.fn() })
    const root = { dataset: {} as DOMStringMap }
    expect(preferences.load()).toBeNull()
    createBrowserThemeTarget(root).apply('graphite-signal')
    expect(root.dataset.theme).toBe('graphite-signal')
  })
})
