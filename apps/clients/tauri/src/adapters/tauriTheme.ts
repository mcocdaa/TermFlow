import { load } from '@tauri-apps/plugin-store'
import { createThemeState, isThemeId, THEME_STORAGE_KEY, type ThemeTarget } from '@termflow/client-ui'

export async function createTauriThemeState(root: HTMLElement = document.documentElement) {
  const store = await load('preferences.json', { autoSave: false, defaults: {} })
  const stored = await store.get<string>(THEME_STORAGE_KEY)
  const initial = stored ?? null
  const target: ThemeTarget = {
    apply(theme) { root.dataset.theme = theme },
  }
  return createThemeState({
    load: () => isThemeId(initial) ? initial : null,
    save: (theme) => {
      void store.set(THEME_STORAGE_KEY, theme).then(() => store.save())
    },
  }, target)
}
