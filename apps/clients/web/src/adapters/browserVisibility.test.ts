import { describe, expect, it, vi } from 'vitest'
import { createBrowserVisibility } from './browserVisibility'

describe('browser visibility adapter', () => {
  it('reports hidden state and releases the browser listener', () => {
    let hidden = false
    let browserListener: (() => void) | undefined
    const source = {
      isHidden: () => hidden,
      subscribe: vi.fn((listener: () => void) => { browserListener = listener }),
      unsubscribe: vi.fn(),
    }
    const visibility = createBrowserVisibility(source)
    const listener = vi.fn()
    const unsubscribe = visibility.subscribe(listener)

    expect(visibility.isHidden()).toBe(false)
    hidden = true
    browserListener?.()
    expect(visibility.isHidden()).toBe(true)
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
    expect(source.unsubscribe).toHaveBeenCalledWith(browserListener)
  })
})
