import { describe, expect, it } from 'vitest'
import { setTauriPlatformAttribute } from './tauriPlatformAttribute'

describe('setTauriPlatformAttribute', () => {
  it('marks Android and removes stale platform markers elsewhere', () => {
    const root = document.documentElement

    setTauriPlatformAttribute(root, 'android')
    expect(root.dataset.tauriPlatform).toBe('android')

    setTauriPlatformAttribute(root, 'windows')
    expect(root.dataset.tauriPlatform).toBeUndefined()
  })
})
