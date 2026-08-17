import { describe, expect, it } from 'vitest'
import { createAppRouter } from './router'

describe('browser router composition', () => {
  it('redirects protected pages to login and retains the intended route', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }) })

    await router.push('/terms/term-7')
    await router.isReady()

    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-7')
  })
})
