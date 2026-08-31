import type { CredentialVaultPort, NativeAccessCredential } from './ports'

export interface NativeTokenSessionOptions {
  issuer: string
  vault: CredentialVaultPort
  refresh(issuer: string): Promise<NativeAccessCredential>
  now?: () => number
  refreshEarlyMs?: number
}

function invalidGrant(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'invalid_grant'
}

export class NativeTokenSession {
  private refreshInFlight: Promise<NativeAccessCredential> | null = null

  constructor(private readonly options: NativeTokenSessionOptions) {}

  async accessToken(): Promise<string> {
    const current = await this.options.vault.load(this.options.issuer)
    const now = this.options.now?.() ?? Date.now()
    const early = this.options.refreshEarlyMs ?? 60_000
    if (current !== null && Date.parse(current.expiresAt) - early > now) return current.accessToken

    this.refreshInFlight ??= this.refresh()
    try {
      return (await this.refreshInFlight).accessToken
    } finally {
      this.refreshInFlight = null
    }
  }

  private async refresh(): Promise<NativeAccessCredential> {
    try {
      const next = await this.options.refresh(this.options.issuer)
      await this.options.vault.replace(this.options.issuer, next)
      return next
    } catch (error) {
      if (invalidGrant(error)) {
        await this.options.vault.clear(this.options.issuer)
        throw new Error('native_authorization_required')
      }
      throw error
    }
  }
}
