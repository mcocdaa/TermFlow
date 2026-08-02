import { afterEach, vi } from 'vitest'

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
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
