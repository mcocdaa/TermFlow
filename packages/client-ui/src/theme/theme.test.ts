import { describe, expect, it, vi } from 'vitest'
import type { ThemeId } from '@termflow/design-tokens'
import { createThemeState, isThemeId, THEME_STORAGE_KEY, type ThemePreferences, type ThemeTarget } from './theme'

function setup(stored: ThemeId | null) {
  const preferences: ThemePreferences = { load: vi.fn(() => stored), save: vi.fn() }
  const target: ThemeTarget = { apply: vi.fn() }
  return { preferences, target, theme: createThemeState(preferences, target) }
}

describe('theme state', () => {
  it('exposes the shared theme identifier validator to platform adapters', () => {
    expect(isThemeId('cloud-cobalt')).toBe(true)
    expect(isThemeId('admin-token-secret')).toBe(false)
    expect(isThemeId(null)).toBe(false)
  })

  it('loads and applies a valid preference without exposing credentials', () => {
    const { theme, target, preferences } = setup('cloud-cobalt')
    expect(theme.active.value).toBe('cloud-cobalt')
    expect(target.apply).toHaveBeenCalledWith('cloud-cobalt')
    expect(THEME_STORAGE_KEY).toBe('termflow.theme')
    expect(preferences).not.toHaveProperty('token')
  })

  it('falls back safely and persists selection only through the preference port', () => {
    const { theme, target, preferences } = setup(null)
    expect(theme.active.value).toBe('graphite-signal')
    theme.select('midnight-indigo')
    expect(theme.active.value).toBe('midnight-indigo')
    expect(preferences.save).toHaveBeenCalledWith('midnight-indigo')
    expect(target.apply).toHaveBeenLastCalledWith('midnight-indigo')
  })

  it('keeps independent theme state for separate client applications', () => {
    const first = setup('cloud-cobalt')
    const second = setup('midnight-indigo')

    first.theme.select('graphite-signal')

    expect(first.theme.active.value).toBe('graphite-signal')
    expect(second.theme.active.value).toBe('midnight-indigo')
    expect(first.preferences.save).toHaveBeenCalledWith('graphite-signal')
    expect(second.preferences.save).not.toHaveBeenCalled()
  })
})
