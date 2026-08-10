import { arch, platform } from '@tauri-apps/plugin-os'
import { DeviceAuthorizationSession, NativeAuthorizationSession, createPkce, type DeviceAuthorizationPollResponse } from '@termflow/client-core'
import type { OAuthScope } from '@termflow/client-contracts'
import type { OAuthDeviceCodeResponse, OAuthPublicJwk } from '@termflow/client-contracts'
import type { ClientRuntime } from '@termflow/client-ui'
import { createTauriKey, exchangeAuthorization, tauriAuthorizationBrowser } from './adapters/tauriAuthorization'
import { createTauriCredentialVault } from './adapters/tauriCredentialVault'
import { buildVersion } from './buildVersion'
import { serverConfig } from './serverConfig'
import { logNativeEvent, sanitizeNativeDetail } from './diagnostics'

const vault = createTauriCredentialVault()

export async function verifyNativeConnection(runtime: Pick<ClientRuntime, 'api'>): Promise<void> {
  await runtime.api.sessions.status()
}

function sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(new DOMException('The device authorization was cancelled.', 'AbortError'))
  }
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(done, milliseconds)
    const onAbort = () => {
      globalThis.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      reject(new DOMException('The device authorization was cancelled.', 'AbortError'))
    }
    function done() {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

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
    browser: tauriAuthorizationBrowser({
      issuer: serverConfig.current,
      loopback: platform() !== 'ios' && platform() !== 'android',
    }),
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
    void logNativeEvent({ event: 'token_exchange_failed', issuer, level: 'error', errorCode: 'authorization_failed', errorDetail: sanitizeNativeDetail(error) })
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
  poll(input: { deviceCode: string; codeVerifier: string; publicJwk: OAuthPublicJwk }, signal?: AbortSignal): Promise<DeviceAuthorizationPollResponse>
}

export async function beginNativeDeviceAuthorization(input: NativeDeviceAuthorizationInput) {
  await serverConfig.replace(input.issuer)
  const key = createTauriKey(serverConfig.current)
  const [pkce, publicJwk, dpopJkt] = await Promise.all([createPkce(cryptoPort), key.publicJwk(), key.thumbprint()])
  let response: OAuthDeviceCodeResponse
  try {
    response = await input.create({
      clientName: input.client.name,
      platform: input.client.platform,
      clientVersion: input.client.version,
      codeChallenge: pkce.challenge,
      dpopJkt,
      publicJwk,
      scopes: input.scopes,
    })
  } catch (error) {
    void logNativeEvent({ event: 'device_code_request_failed', issuer: input.issuer, level: 'error', errorCode: 'device_code_request_failed', errorDetail: sanitizeNativeDetail(error) })
    throw error
  }
  const session = new DeviceAuthorizationSession({
    issuer: serverConfig.current,
    deviceCode: response.device_code,
    codeVerifier: pkce.verifier,
    publicJwk,
    interval: response.interval,
    poll: input.poll,
    vault,
    sleep,
  })
  return { response, session }
}
