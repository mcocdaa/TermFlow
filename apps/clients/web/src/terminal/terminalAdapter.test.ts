import { describe, expect, it } from 'vitest'
import { visualFontSize } from './terminalAdapter'

describe('xterm visual font scaling', () => {
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
