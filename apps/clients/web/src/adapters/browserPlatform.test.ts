import { describe, expect, it } from 'vitest'
import { browserPlatform } from './browserPlatform'

describe('browser platform adapter', () => {
  it('exposes only the platform name needed by xterm selection behavior', () => {
    expect(browserPlatform({ platform: 'MacIntel' })).toBe('MacIntel')
  })
})
