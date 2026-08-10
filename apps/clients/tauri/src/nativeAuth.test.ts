import { ApiError } from '@termflow/client-core'
import type { ClientRuntime } from '@termflow/client-ui'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { verifyNativeConnection } from './nativeAuth'

vi.mock('@tauri-apps/plugin-os', () => ({ platform: () => 'linux', arch: () => 'x64' }))
vi.mock('./serverConfig', () => ({
  serverConfig: { current: 'https://relay.example.com', replace: vi.fn().mockResolvedValue(undefined) },
}))
vi.mock('./adapters/tauriCredentialVault', () => ({ createTauriCredentialVault: () => ({ load: vi.fn(), replace: vi.fn(), clear: vi.fn() }) }))
vi.mock('./adapters/tauriAuthorization', () => ({
  createTauriKey: vi.fn(),
  exchangeAuthorization: vi.fn(),
  tauriAuthorizationBrowser: vi.fn(),
  pollDeviceAuthorization: vi.fn(),
}))
vi.mock('./buildVersion', () => ({ buildVersion: '0.0.1-test' }))
vi.mock('./diagnostics', () => ({ logNativeEvent: vi.fn(), sanitizeNativeDetail: (value: unknown) => String(value) }))

beforeEach(() => { vi.clearAllMocks() })

describe('verifyNativeConnection', () => {
  it('calls the protected session status through the runtime', async () => {
    const status = vi.fn().mockResolvedValue({ authenticated: true, expires_at: null })

    await verifyNativeConnection({ api: { sessions: { status } } } as unknown as Pick<ClientRuntime, 'api'>)

    expect(status).toHaveBeenCalledTimes(1)
  })

  it('propagates capability and offline rejections', async () => {
    await expect(
      verifyNativeConnection({
        api: { sessions: { status: vi.fn().mockRejectedValue(new ApiError('http_capability_denied')) } },
      } as unknown as Pick<ClientRuntime, 'api'>),
    ).rejects.toMatchObject({ kind: 'http_capability_denied' })

    await expect(
      verifyNativeConnection({
        api: { sessions: { status: vi.fn().mockRejectedValue(new ApiError('offline')) } },
      } as unknown as Pick<ClientRuntime, 'api'>),
    ).rejects.toMatchObject({ kind: 'offline' })
  })
})
