import { afterEach, vi } from 'vitest'

function createStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key) },
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverStub, writable: true })
Object.defineProperty(globalThis, 'localStorage', { value: createStorage(), writable: true })
Object.defineProperty(globalThis, 'sessionStorage', { value: createStorage(), writable: true })
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
  localStorage.clear()
  sessionStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
