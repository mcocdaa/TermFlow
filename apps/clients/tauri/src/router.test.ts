import { clientRoutes, type ClientRuntime } from '@termflow/client-ui'
import { describe, expect, it, vi } from 'vitest'
import { createTauriRouter } from './router'

vi.mock('@xterm/xterm', () => ({ Terminal: class {} }))

describe('Tauri router composition', () => {
  it('filters every Web-only route from the native client', () => {
    const runtime = {
      api: { dashboard: { get: async () => ({}) } },
    } as unknown as ClientRuntime
    const router = createTauriRouter(runtime)
    const webOnlyPaths = clientRoutes.filter((route) => route.meta?.webOnly === true).map((route) => route.path)
    const nativePaths = router.getRoutes().map((route) => route.path)

    expect(webOnlyPaths).toContain('/settings/two-factor-auth')
    expect(nativePaths).not.toContain('/settings/two-factor-auth')
  })
})
