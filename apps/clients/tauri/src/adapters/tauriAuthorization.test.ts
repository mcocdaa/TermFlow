import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  onOpenUrl: vi.fn(),
  openUrl: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))
vi.mock('@tauri-apps/plugin-deep-link', () => ({ onOpenUrl: mocks.onOpenUrl }))
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: mocks.openUrl }))

import { tauriAuthorizationBrowser } from './tauriAuthorization'

describe('tauriAuthorizationBrowser', () => {
  it('registers the deep-link listener before opening the system browser', async () => {
    let receive: ((urls: string[]) => void) | undefined
    let registrationReady: ((unlisten: () => void) => void) | undefined
    mocks.onOpenUrl.mockImplementation((callback: (urls: string[]) => void) => {
      receive = callback
      return new Promise(resolve => { registrationReady = resolve })
    })
    mocks.openUrl.mockResolvedValue(undefined)

    const callback = tauriAuthorizationBrowser.waitForCallback('state-1')
    const opened = tauriAuthorizationBrowser.open('https://b.example/api/v1/oauth/authorize')
    await Promise.resolve()
    expect(mocks.openUrl).not.toHaveBeenCalled()

    registrationReady?.(vi.fn())
    await opened
    expect(mocks.openUrl).toHaveBeenCalledOnce()

    receive?.(['termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111'])
    await expect(callback).resolves.toContain('transaction_id=')
  })
})
