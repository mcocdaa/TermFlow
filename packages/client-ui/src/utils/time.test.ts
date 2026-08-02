import { describe, expect, it } from 'vitest'
import { formatBRecordedTime } from './time'

describe('formatBRecordedTime', () => {
  it('uses the C browser local timezone without rendering a timezone suffix', () => {
    const rendered = formatBRecordedTime('2026-08-01T03:21:00Z')

    expect(rendered).toMatch(/^2026年\d{1,2}月\d{1,2}日 \d{2}:\d{2}$/)
    expect(rendered).not.toMatch(/GMT|UTC|CST/i)
  })
})
