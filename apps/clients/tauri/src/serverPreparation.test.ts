import type { OAuthMetadataResponse } from '@termflow/client-contracts'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { prepareNativeServer } from './serverPreparation'

const mocks = vi.hoisted(() => ({
  logNativeEvent: vi.fn(),
  replaceServer: vi.fn(),
}))

vi.mock('./diagnostics', () => ({ logNativeEvent: mocks.logNativeEvent }))
vi.mock('./serverConfig', () => ({
  canonicalIssuer: (value: string) => new URL(value).origin,
  serverConfig: { replace: mocks.replaceServer },
}))

function metadataFor(issuer: string): OAuthMetadataResponse {
  return {
    issuer,
    authorization_endpoint: `${issuer}/api/v1/oauth/authorize`,
    token_endpoint: `${issuer}/api/v1/oauth/token`,
    revocation_endpoint: `${issuer}/api/v1/oauth/revoke`,
    device_authorization_endpoint: `${issuer}/api/v1/oauth/device/code`,
    device_verification_uri: `${issuer}/device`,
    response_types_supported: ['code'],
    grant_types_supported: ['authorization_code', 'refresh_token', 'urn:ietf:params:oauth:grant-type:device_code'],
    code_challenge_methods_supported: ['S256'],
    dpop_signing_alg_values_supported: ['ES256'],
    scopes_supported: ['terminal.read'],
  }
}

beforeEach(() => {
  mocks.logNativeEvent.mockReset().mockResolvedValue(undefined)
  mocks.replaceServer.mockReset().mockResolvedValue(undefined)
})

describe('prepareNativeServer', () => {
  it('persists the canonical issuer before loading metadata', async () => {
    const calls: string[] = []
    mocks.replaceServer.mockImplementation(async (issuer: string) => { calls.push(`replace:${issuer}`) })
    const loadMetadata = vi.fn(async () => {
      calls.push('metadata')
      return metadataFor('https://termflow.example')
    })

    await expect(prepareNativeServer('https://termflow.example/', loadMetadata))
      .resolves.toEqual({
        issuer: 'https://termflow.example',
        metadata: metadataFor('https://termflow.example'),
      })
    expect(calls).toEqual(['replace:https://termflow.example', 'metadata'])
    expect(mocks.logNativeEvent).toHaveBeenCalledWith({
      event: 'metadata_succeeded',
      issuer: 'https://termflow.example',
    })
  })

  it('rejects metadata from another issuer without logging response details', async () => {
    await expect(prepareNativeServer(
      'https://termflow.example',
      async () => metadataFor('https://attacker.example'),
    )).rejects.toThrow('issuer_mismatch')

    expect(mocks.logNativeEvent).toHaveBeenLastCalledWith({
      event: 'metadata_failed',
      issuer: 'https://termflow.example',
      level: 'error',
      errorCode: 'issuer_mismatch',
    })
    expect(JSON.stringify(mocks.logNativeEvent.mock.calls)).not.toContain('attacker.example')
  })
})
