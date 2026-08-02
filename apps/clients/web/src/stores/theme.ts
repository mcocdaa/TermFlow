import { activeTheme, configureActiveTheme, selectActiveTheme, THEME_STORAGE_KEY } from '@termflow/client-ui'
import type { ThemeId } from '@termflow/design-tokens'
import { createBrowserThemePreferences, createBrowserThemeTarget } from '../adapters/browserThemePreferences'

export { THEME_STORAGE_KEY }
export { activeTheme }
let configured = false

export function applyInitialTheme(storage?: Pick<Storage, 'getItem' | 'setItem'>): ThemeId {
  const preferences = storage === undefined ? createBrowserThemePreferences() : createBrowserThemePreferences(storage)
  configured = true
  return configureActiveTheme(preferences, createBrowserThemeTarget())
}

export function selectTheme(theme: ThemeId): void {
  if (!configured) applyInitialTheme()
  selectActiveTheme(theme)
}
