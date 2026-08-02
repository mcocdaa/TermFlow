import { describe, expect, it } from 'vitest'
import { createOrientationViewState, orientationFor } from './orientation'

describe('terminal orientation state', () => {
  it.each([
    [360, 800, 'portrait'],
    [800, 360, 'landscape'],
    [1024, 768, 'landscape'],
    [1440, 900, 'landscape'],
  ] as const)('classifies %ix%i as %s', (width, height, expected) => {
    expect(orientationFor(width, height)).toBe(expected)
  })

  it('keeps portrait and landscape display and viewport choices independent', () => {
    const state = createOrientationViewState()
    state.portrait.displayMode = 'scale-50'
    state.portrait.viewport = { scale: 2, panX: -30, panY: -40 }
    expect(state.landscape).toEqual({ displayMode: 'fit', viewport: null })
  })
})
