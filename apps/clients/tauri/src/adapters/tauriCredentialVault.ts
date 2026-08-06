import { invoke } from '@tauri-apps/api/core'
import type { CredentialVaultPort, NativeAccessCredential } from '@termflow/client-core'
import { createMemoryAccessVault } from './memoryAccessVault'

export async function clearNativeCredentials(issuer: string): Promise<void> {
  await invoke('native_clear_credentials', { issuer })
}

/**
 * Access tokens are process-local. Clearing a native session also asks Rust to
 * delete its refresh token from the platform keyring; this adapter never reads
 * keyring material into the WebView.
 */
export function createTauriCredentialVault(): CredentialVaultPort {
  const memory = createMemoryAccessVault()
  return {
    load: (issuer) => memory.load(issuer),
    replace: (issuer, value: NativeAccessCredential) => memory.replace(issuer, value),
    async clear(issuer) {
      await memory.clear(issuer)
      await clearNativeCredentials(issuer)
    },
  }
}
