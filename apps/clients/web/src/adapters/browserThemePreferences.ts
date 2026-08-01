import {
  THEME_STORAGE_KEY,
  type ThemePreferences,
  type ThemeTarget,
} from '@termflow/client-ui'
import { themeIds, type ThemeId } from '@termflow/design-tokens'

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>
type ThemeRoot = Pick<HTMLElement, 'dataset'>

function isThemeId(value: string | null): value is ThemeId {
  return value !== null && (themeIds as readonly string[]).includes(value)
}

export function createBrowserThemePreferences(storage: ThemeStorage = globalThis.localStorage): ThemePreferences {
  return {
    load() {
      const stored = storage.getItem(THEME_STORAGE_KEY)
      return isThemeId(stored) ? stored : null
    },
    save(theme) { storage.setItem(THEME_STORAGE_KEY, theme) },
  }
}

export function createBrowserThemeTarget(root: ThemeRoot = document.documentElement): ThemeTarget {
  return { apply(theme) { root.dataset.theme = theme } }
}
