import { createThemeState, THEME_STORAGE_KEY } from '@termflow/client-ui'
import type { ThemeId } from '@termflow/design-tokens'
import { ref } from 'vue'
import { createBrowserThemePreferences, createBrowserThemeTarget } from '../adapters/browserThemePreferences'

export { THEME_STORAGE_KEY }
export const activeTheme = ref<ThemeId>('graphite-signal')

let selectCurrentTheme: ((theme: ThemeId) => void) | null = null

export function applyInitialTheme(storage?: Pick<Storage, 'getItem' | 'setItem'>): ThemeId {
  const preferences = storage === undefined ? createBrowserThemePreferences() : createBrowserThemePreferences(storage)
  const state = createThemeState(preferences, createBrowserThemeTarget())
  activeTheme.value = state.active.value
  selectCurrentTheme = (theme) => {
    state.select(theme)
    activeTheme.value = state.active.value
  }
  return activeTheme.value
}

export function selectTheme(theme: ThemeId): void {
  if (selectCurrentTheme === null) applyInitialTheme()
  selectCurrentTheme?.(theme)
}
