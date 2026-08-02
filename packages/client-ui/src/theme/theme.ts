import { themeIds, type ThemeId } from '@termflow/design-tokens'
import { inject, readonly, ref, type DeepReadonly, type Ref } from 'vue'
import { themeStateKey } from '../runtimeKey'

export type { ThemeId } from '@termflow/design-tokens'

export const THEME_STORAGE_KEY = 'termflow.theme'
const DEFAULT_THEME: ThemeId = 'graphite-signal'

export interface ThemePreferences {
  load(): ThemeId | null
  save(theme: ThemeId): void
}

export interface ThemeTarget {
  apply(theme: ThemeId): void
}

export interface ThemeState {
  readonly active: DeepReadonly<Ref<ThemeId>>
  select(theme: ThemeId): void
}

export function isThemeId(value: string | null): value is ThemeId {
  return value !== null && (themeIds as readonly string[]).includes(value)
}

export function createThemeState(preferences: ThemePreferences, target: ThemeTarget): ThemeState {
  const stored = preferences.load()
  const initial = isThemeId(stored) ? stored : DEFAULT_THEME
  const active = ref<ThemeId>(initial)
  target.apply(initial)
  return {
    active: readonly(active),
    select(theme) {
      active.value = theme
      target.apply(theme)
      preferences.save(theme)
    },
  }
}

export function useTheme(): ThemeState {
  const theme = inject(themeStateKey, undefined)
  if (theme === undefined) throw new Error('TermFlow client theme is not installed.')
  return theme
}
