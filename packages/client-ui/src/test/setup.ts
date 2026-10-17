import { afterEach, vi } from 'vitest'
import { configureLocalePreferences, LOCALE_STORAGE_KEY, setLocale, type Locale } from '../i18n'

if (typeof window !== 'undefined' && typeof window.localStorage?.getItem !== 'function') {
  const store = new Map<string, string>()
  const memoryStorage: Storage = {
    get length() {
      return store.size
    },
    clear() {
      store.clear()
    },
    getItem(key: string) {
      return store.get(key) ?? null
    },
    key(index: number) {
      return [...store.keys()][index] ?? null
    },
    removeItem(key: string) {
      store.delete(key)
    },
    setItem(key: string, value: string) {
      store.set(key, String(value))
    },
  }
  Object.defineProperty(window, 'localStorage', { value: memoryStorage, configurable: true, writable: true })
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage, configurable: true, writable: true })
}

configureLocalePreferences({
  load() {
    const v = window.localStorage?.getItem(LOCALE_STORAGE_KEY)
    return v === 'en' || v === 'zh' ? v : null
  },
  save(loc: Locale) {
    window.localStorage?.setItem(LOCALE_STORAGE_KEY, loc)
  },
})

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverStub, writable: true })
Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
  value: vi.fn(() => ({
    fillStyle: '',
    fillRect: vi.fn(),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) })),
  })),
  writable: true,
})
Object.defineProperty(globalThis, 'matchMedia', {
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
  writable: true,
})

afterEach(() => {
  setLocale('zh')
  try {
    if (typeof window.localStorage?.removeItem === 'function') {
      window.localStorage.removeItem('termflow_lang')
    }
  } catch {}
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
