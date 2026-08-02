import { describe, expect, it } from 'vitest'
import { browserCanonicalServerUrl } from './browserCanonicalServerUrl'

describe('browser canonical server URL adapter', () => {
  it('uses only the browser origin and omits paths, query strings, and fragments', () => {
    expect(browserCanonicalServerUrl({ origin: 'https://b.termflow.test' })).toBe('https://b.termflow.test')
  })
})
