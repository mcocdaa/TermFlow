import { ref } from 'vue'
import { themeIds, type ThemeId } from '@termflow/design-tokens'

export const THEME_STORAGE_KEY = 'termflow.theme'
export const activeTheme = ref<ThemeId>('graphite-signal')

function isThemeId(value: string | null): value is ThemeId {
  return value !== null && (themeIds as readonly string[]).includes(value)
}

export function applyInitialTheme(storage: Pick<Storage, 'getItem'> = localStorage): ThemeId {
  const stored = storage.getItem(THEME_STORAGE_KEY)
  activeTheme.value = isThemeId(stored) ? stored : 'graphite-signal'
  document.documentElement.dataset.theme = activeTheme.value
  return activeTheme.value
}

export function selectTheme(theme: ThemeId, storage: Pick<Storage, 'setItem'> = localStorage) {
  activeTheme.value = theme
  document.documentElement.dataset.theme = theme
  storage.setItem(THEME_STORAGE_KEY, theme)
}
