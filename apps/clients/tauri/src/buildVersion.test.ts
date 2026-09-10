import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { buildVersion } from './buildVersion'

const manifest = JSON.parse(
  readFileSync(resolve(process.cwd(), 'package.json'), 'utf8'),
) as { version: string }

describe('buildVersion', () => {
  it('comes from the materialized native package manifest', () => {
    expect(buildVersion).toBe(manifest.version)
  })
})
