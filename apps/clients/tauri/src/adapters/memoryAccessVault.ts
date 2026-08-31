import type { CredentialVaultPort, NativeAccessCredential } from '@termflow/client-core'

export function createMemoryAccessVault(): CredentialVaultPort {
  const values = new Map<string, NativeAccessCredential>()
  return {
    load: async (issuer) => values.get(issuer) ?? null,
    replace: async (issuer, value) => { values.set(issuer, value) },
    clear: async (issuer) => { values.delete(issuer) },
  }
}
