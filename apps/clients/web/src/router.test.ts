import { clientRoutes } from '@termflow/client-ui'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from './router'

describe('browser router composition', () => {
  it('installs the shared route table', () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })

    expect(router.getRoutes().map((route) => route.path).sort()).toEqual(
      clientRoutes.map((route) => route.path).sort(),
    )
  })

  it('redirects protected pages to login and retains the intended route', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }) })

    await router.push('/terms/term-7')
    await router.isReady()

    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-7')
  })
})
