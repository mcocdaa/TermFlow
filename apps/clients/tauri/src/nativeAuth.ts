import { arch, platform } from '@tauri-apps/plugin-os'
import { DeviceAuthorizationSession, NativeAuthorizationSession, createPkce } from '@termflow/client-core'
import type { OAuthScope } from '@termflow/client-contracts'
import type { OAuthDeviceCodeResponse, OAuthPublicJwk, OAuthTokenResponse } from '@termflow/client-contracts'
import { createMemoryAccessVault } from './adapters/memoryAccessVault'
import { createTauriKey, exchangeAuthorization, tauriAuthorizationBrowser } from './adapters/tauriAuthorization'
import { buildVersion } from './buildVersion'
import { serverConfig } from './serverConfig'
import { logNativeEvent } from './diagnostics'

const vault = createMemoryAccessVault()
const cryptoPort = {
  randomBytes(length: number) { const value = new Uint8Array(length); globalThis.crypto.getRandomValues(value); return value },
  async sha256(input: Uint8Array) { return new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', input.slice().buffer)) },
}

export async function authorizeNativeClient(issuer: string, authorizeEndpoint: string, scopes: OAuthScope[]) {
  void logNativeEvent({ event: 'connect_started', issuer })
  await serverConfig.replace(issuer)
  const key = createTauriKey(serverConfig.current)
  const session = new NativeAuthorizationSession({
    issuer: serverConfig.current,
    authorizeEndpoint,
    client: { name: 'TermFlow', platform: `${platform()} ${arch()}`, version: buildVersion },
    scopes,
    browser: tauriAuthorizationBrowser,
    vault,
    key,
    createPkce: () => createPkce(cryptoPort),
    createId: () => globalThis.crypto.randomUUID(),
    exchange: ({ issuer: target, transaction, verifier, redirectUri }) => exchangeAuthorization({ issuer: target, transaction, verifier, redirectUri }),
  })
  try {
    const credential = await session.authorize()
    void logNativeEvent({ event: 'token_exchange_succeeded', issuer })
    return credential
  } catch (error) {
    void logNativeEvent({ event: 'token_exchange_failed', issuer, level: 'error', errorCode: 'authorization_failed' })
    throw error
  }
}

export interface NativeDeviceAuthorizationInput {
  issuer: string
  scopes: OAuthScope[]
  client: { name: string; platform: string; version: string }
  create(input: {
    clientName: string
    platform: string
    clientVersion: string
    codeChallenge: string
    dpopJkt: string
    publicJwk: OAuthPublicJwk
    scopes: OAuthScope[]
  }): Promise<OAuthDeviceCodeResponse>
  poll(input: { deviceCode: string; codeVerifier: string; publicJwk: OAuthPublicJwk }, signal?: AbortSignal): Promise<OAuthTokenResponse>
}

export async function beginNativeDeviceAuthorization(input: NativeDeviceAuthorizationInput) {
  await serverConfig.replace(input.issuer)
  const key = createTauriKey(serverConfig.current)
  const [pkce, publicJwk, dpopJkt] = await Promise.all([createPkce(cryptoPort), key.publicJwk(), key.thumbprint()])
  const response = await input.create({
    clientName: input.client.name,
    platform: input.client.platform,
    clientVersion: input.client.version,
    codeChallenge: pkce.challenge,
    dpopJkt,
    publicJwk,
    scopes: input.scopes,
  })
  const session = new DeviceAuthorizationSession({
    issuer: serverConfig.current,
    deviceCode: response.device_code,
    codeVerifier: pkce.verifier,
    publicJwk,
    interval: response.interval,
    poll: input.poll,
    vault,
  })
  return { response, session }
}
