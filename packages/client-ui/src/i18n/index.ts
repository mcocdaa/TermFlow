import { setApiErrorLocale } from '@termflow/client-core'
import { readonly, ref, type DeepReadonly, type Ref } from 'vue'
import { enMessages } from './messages/en'
import { zhMessages } from './messages/zh'

export type Locale = 'zh' | 'en'
export const LOCALE_STORAGE_KEY = 'termflow_lang'
export { enMessages, zhMessages }

export interface LocalePreferences {
  load(): Locale | null
  save(locale: Locale): void
}

let localePreferences: LocalePreferences = {
  load: () => null,
  save: () => undefined,
}

function readStoredLocale(): Locale | null {
  try {
    const stored = localePreferences.load()
    if (stored === 'en' || stored === 'zh') return stored
  } catch {
    // Ignore preference access errors in restricted environments.
  }
  return null
}

const currentLocale = ref<Locale>(readStoredLocale() ?? 'zh')
setApiErrorLocale(currentLocale.value)

export function configureLocalePreferences(prefs: LocalePreferences): void {
  localePreferences = prefs
  const stored = readStoredLocale()
  if (stored !== null) {
    setLocale(stored)
  }
}

const messages: Record<Locale, Record<string, string>> = {
  zh: zhMessages,
  en: enMessages,
}

export const locale: DeepReadonly<Ref<Locale>> = readonly(currentLocale)

export function setLocale(next: Locale): void {
  currentLocale.value = next
  setApiErrorLocale(next)
  try {
    localePreferences.save(next)
  } catch {
    // Ignore preference persistence errors.
  }
}

export function toggleLocale(): Locale {
  const next: Locale = currentLocale.value === 'zh' ? 'en' : 'zh'
  setLocale(next)
  return next
}

export function t(key: string, params?: Record<string, string | number>): string {
  const dict = messages[currentLocale.value] ?? messages.zh
  const template = dict[key] ?? messages.zh[key] ?? key
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (_, token: string) => {
    const val = params[token]
    return val === undefined ? `{${token}}` : String(val)
  })
}

const reverseKeyMap = new Map<string, string>()
for (const [key, val] of Object.entries(messages.zh)) {
  if (!val.includes('{')) reverseKeyMap.set(val, key)
}
for (const [key, val] of Object.entries(messages.en)) {
  if (!val.includes('{')) reverseKeyMap.set(val, key)
}

export function translateDynamicText(text: string): string {
  if (!text) return text
  const dict = messages[currentLocale.value] ?? messages.zh
  if (dict[text] !== undefined) return dict[text]
  const matchedKey = reverseKeyMap.get(text)
  if (matchedKey !== undefined && dict[matchedKey] !== undefined) {
    return dict[matchedKey]
  }
  return text
}

export function useI18n() {
  const stored = readStoredLocale()
  if (stored !== null && currentLocale.value !== stored) {
    currentLocale.value = stored
    setApiErrorLocale(stored)
  }
  return {
    locale,
    setLocale,
    toggleLocale,
    t,
    translateDynamicText,
  }
}
