import type { ClockPort } from '@termflow/client-ui'

export interface BrowserClockSource {
  now(): number
  setTimeout(callback: () => void, delayMs: number): unknown
  clearTimeout(handle: unknown): void
  setInterval(callback: () => void, delayMs: number): unknown
  clearInterval(handle: unknown): void
}

function browserClockSource(): BrowserClockSource {
  return {
    now: () => Date.now(),
    setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    clearTimeout: (handle) => globalThis.clearTimeout(handle as number),
    setInterval: (callback, delayMs) => globalThis.setInterval(callback, delayMs),
    clearInterval: (handle) => globalThis.clearInterval(handle as number),
  }
}

export function createBrowserClock(source: BrowserClockSource = browserClockSource()): ClockPort {
  return {
    now: () => source.now(),
    setTimeout: (callback, delayMs) => source.setTimeout(callback, delayMs),
    clearTimeout: (handle) => source.clearTimeout(handle),
    setInterval: (callback, delayMs) => source.setInterval(callback, delayMs),
    clearInterval: (handle) => source.clearInterval(handle),
  }
}
