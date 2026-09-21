import { describe, expect, it } from 'vitest'
import { isUnifiedDiff, parseUnifiedDiff } from './diff'

describe('diff utils', () => {
  it('detects unified diff headers and hunks', () => {
    const diffSample = `--- a/src/index.ts
+++ b/src/index.ts
@@ -1,3 +1,4 @@
 import { foo } from './foo'
-const a = 1
+const a = 2
+const b = 3
`
    expect(isUnifiedDiff(diffSample)).toBe(true)
    const lines = parseUnifiedDiff(diffSample)
    expect(lines[0]?.type).toBe('header')
    expect(lines[1]?.type).toBe('header')
    expect(lines[2]?.type).toBe('hunk')
    expect(lines[3]?.type).toBe('normal')
    expect(lines[4]?.type).toBe('del')
    expect(lines[5]?.type).toBe('add')
    expect(lines[6]?.type).toBe('add')
  })

  it('rejects regular text that is not a diff', () => {
    expect(isUnifiedDiff('echo "hello world"')).toBe(false)
    expect(isUnifiedDiff('total 12\ndrwxr-xr-x 2 user user')).toBe(false)
    expect(isUnifiedDiff('')).toBe(false)
  })
})
