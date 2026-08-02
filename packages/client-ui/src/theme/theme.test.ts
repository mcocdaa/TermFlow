import { describe, expect, it, vi } from 'vitest'
import type { ThemeId } from '@termflow/design-tokens'
import { activeTheme, configureActiveTheme, createThemeState, selectActiveTheme, THEME_STORAGE_KEY, type ThemePreferences, type ThemeTarget } from './theme'

function setup(stored: ThemeId | null) {
  const preferences: ThemePreferences = { load: vi.fn(() => stored), save: vi.fn() }
  const target: ThemeTarget = { apply: vi.fn() }
  return { preferences, target, theme: createThemeState(preferences, target) }
}

describe('theme state', () => {
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

  it('exposes one configured active theme for shared terminal and settings UI', () => {
    const preferences: ThemePreferences = { load: vi.fn<() => ThemeId | null>(() => 'cloud-cobalt'), save: vi.fn() }
    const target: ThemeTarget = { apply: vi.fn() }
    configureActiveTheme(preferences, target)

    expect(activeTheme.value).toBe('cloud-cobalt')
    selectActiveTheme('midnight-indigo')
    expect(activeTheme.value).toBe('midnight-indigo')
    expect(preferences.save).toHaveBeenCalledWith('midnight-indigo')
    expect(target.apply).toHaveBeenLastCalledWith('midnight-indigo')
  })
})
