export interface PublicEcJwk {
  kty: 'EC'
  crv: 'P-256'
  alg: 'ES256'
  x: string
  y: string
  kid?: string
}

export interface NativeKeyPort {
  publicJwk(): Promise<PublicEcJwk>
  thumbprint(): Promise<string>
  /** Return the raw 64-byte JOSE ES256 signature, never the private key. */
  signJwt(signingInput: Uint8Array): Promise<Uint8Array>
}

export interface NativeAccessCredential {
  accessToken: string
  expiresAt: string
  tokenType: 'DPoP'
}

export interface CredentialVaultPort {
  /** Loads only the short-lived access credential; refresh material stays native. */
  load(issuer: string): Promise<NativeAccessCredential | null>
  replace(issuer: string, value: NativeAccessCredential): Promise<void>
  clear(issuer: string): Promise<void>
}

export interface AuthorizationBrowserPort {
  open(url: string): Promise<void>
  waitForCallback(state: string, signal?: AbortSignal): Promise<string>
  /** Prepare the loopback callback target for the state when supported; returns undefined to keep the app-scheme default. */
  prepareCallback?(state: string): Promise<string | undefined>
}

export interface NativeClientDescriptor {
  name: string
  platform: string
  version: string
}

export interface AuthorizationStateListener {
  onState?: (state: AuthorizationState) => void
}
import type { AuthorizationState } from './authorizationState'
