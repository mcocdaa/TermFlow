import { describe, expect, it } from 'vitest'
import { buildVersion } from './buildVersion'

describe('buildVersion', () => {
  it('comes from the materialized native package manifest', () => {
    expect(buildVersion).toBe('0.0.1-dev.0')
  })
})
