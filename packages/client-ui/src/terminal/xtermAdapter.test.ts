import { describe, expect, it } from 'vitest'
import { forceSelectionModifiers, visualFontSize } from './xtermAdapter'

describe('xterm visual font scaling', () => {
  it('uses the same force-selection modifier that xterm expects on each platform', () => {
    expect(forceSelectionModifiers('Linux x86_64', true)).toEqual({ shiftKey: true })
    expect(forceSelectionModifiers('iPhone', true)).toEqual({ shiftKey: true })
    expect(forceSelectionModifiers('MacIntel', true)).toEqual({ altKey: true })
    expect(forceSelectionModifiers('MacIntel', false)).toEqual({})
  })

  it('always derives from the 100% base instead of accumulating scale', () => {
    expect(visualFontSize(14, 0.5)).toBe(7)
    expect(visualFontSize(14, 0.75)).toBe(10.5)
    expect(visualFontSize(14, 1)).toBe(14)
  })

  it('normalizes invalid visual scales without changing terminal geometry', () => {
    expect(visualFontSize(14, 0)).toBe(14)
    expect(visualFontSize(14, Number.NaN)).toBe(14)
  })
})
