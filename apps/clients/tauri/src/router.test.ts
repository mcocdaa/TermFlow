import { describe, expect, it } from 'vitest'
import type { ClientRuntime } from '@termflow/client-ui'
import { createTauriRouter } from './router'

function runtime(): ClientRuntime {
  return { api: { dashboard: { get: async () => ({}) } } } as unknown as ClientRuntime
}

describe('Tauri terminal route query contract', () => {
  it('strips every terminal query except a valid agent UUID', async () => {
    const router = createTauriRouter(runtime())
    await router.push('/terms/term-7?keep=yes&agent=11111111-1111-4111-8111-111111111111&secret=drop-me')
    expect(router.currentRoute.value.query).toEqual({ agent: '11111111-1111-4111-8111-111111111111' })
  })

  it('drops malformed agent values', async () => {
    const router = createTauriRouter(runtime())
    await router.push('/terms/term-7?agent=not-a-uuid')
    expect(router.currentRoute.value.query).toEqual({})
  })
})
