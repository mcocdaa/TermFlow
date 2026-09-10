import { describe, expect, it, vi } from 'vitest'
import { createBrowserClock } from './browserClock'

describe('browser clock adapter', () => {
  it('delegates time and scheduler operations to its browser source', () => {
    const callback = vi.fn()
    const source = {
      now: vi.fn(() => 42),
      setTimeout: vi.fn(() => 7),
      clearTimeout: vi.fn(),
      setInterval: vi.fn(() => 8),
      clearInterval: vi.fn(),
    }
    const clock = createBrowserClock(source)

    expect(clock.now()).toBe(42)
    expect(clock.setTimeout(callback, 100)).toBe(7)
    clock.clearTimeout(7)
    expect(clock.setInterval(callback, 250)).toBe(8)
    clock.clearInterval(8)

    expect(source.setTimeout).toHaveBeenCalledWith(callback, 100)
    expect(source.clearTimeout).toHaveBeenCalledWith(7)
    expect(source.setInterval).toHaveBeenCalledWith(callback, 250)
    expect(source.clearInterval).toHaveBeenCalledWith(8)
  })
})
