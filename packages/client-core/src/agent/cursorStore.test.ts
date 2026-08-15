import { describe, expect, it } from 'vitest'
import { isValidAgentCursor, parseCursorSeq } from './cursorStore'

describe('parseCursorSeq', () => {
  it('extracts the seq component of an opaque cursor', () => {
    expect(parseCursorSeq('7-1')).toBe(1)
    expect(parseCursorSeq('1-0')).toBe(0)
    expect(parseCursorSeq('12-3456')).toBe(3456)
  })

  it('rejects malformed cursors', () => {
    for (const invalid of [
      '', '7', '-1', '7-', '0-1', '007-5', '7--1', 'a-b', ' 7-1', '7-1x', '7-1 ', '7-1-2',
    ]) {
      expect(parseCursorSeq(invalid), invalid).toBeNull()
    }
  })

  it('rejects non-integer or unsafe sequences', () => {
    expect(parseCursorSeq('7-1.5')).toBeNull()
    expect(parseCursorSeq('7-99999999999999999999')).toBeNull()
  })
})

describe('isValidAgentCursor', () => {
  it('accepts the {epoch}-{seq} shape with epoch >= 1 and seq >= 0', () => {
    expect(isValidAgentCursor('1-0')).toBe(true)
    expect(isValidAgentCursor('7-123')).toBe(true)
  })

  it('rejects malformed or out-of-range cursors', () => {
    for (const invalid of [
      '', '7', '-1', '7-', '0-1', '007-5', '7--1', 'a-b', ' 7-1', '7-1x', '7-1 ', '7-1-2', '7--0',
    ]) {
      expect(isValidAgentCursor(invalid), invalid).toBe(false)
    }
  })
})
