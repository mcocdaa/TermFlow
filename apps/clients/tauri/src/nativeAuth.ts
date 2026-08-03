import { arch, platform } from '@tauri-apps/plugin-os'
import { NativeAuthorizationSession, createPkce } from '@termflow/client-core'
import type { OAuthScope } from '@termflow/client-contracts'
import { createMemoryAccessVault } from './adapters/memoryAccessVault'
import { createTauriKey, exchangeAuthorization, tauriAuthorizationBrowser } from './adapters/tauriAuthorization'
import { buildVersion } from './buildVersion'
import { serverConfig } from './serverConfig'

const vault = createMemoryAccessVault()
const cryptoPort = {
  randomBytes(length: number) { const value = new Uint8Array(length); globalThis.crypto.getRandomValues(value); return value },
  async sha256(input: Uint8Array) { return new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', input.slice().buffer)) },
}

export async function authorizeNativeClient(issuer: string, authorizeEndpoint: string, scopes: OAuthScope[]) {
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
  return session.authorize()
}
