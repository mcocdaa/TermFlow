import {
  configureLocalePreferences,
  isThemeId,
  LOCALE_STORAGE_KEY,
  THEME_STORAGE_KEY,
  type Locale,
  type ThemeId,
  type ThemePreferences,
  type ThemeTarget,
} from '@termflow/client-ui'

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>
type ThemeRoot = Pick<HTMLElement, 'dataset'>

export function createBrowserThemePreferences(storage: ThemeStorage = globalThis.localStorage): ThemePreferences {
  configureLocalePreferences({
    load() {
      const v = storage.getItem(LOCALE_STORAGE_KEY)
      return v === 'en' || v === 'zh' ? v : null
    },
    save(loc: Locale) {
      storage.setItem(LOCALE_STORAGE_KEY, loc)
    },
  })
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
