import { invoke } from '@tauri-apps/api/core'

export async function clearNativeCredentials(issuer: string): Promise<void> {
  await invoke('native_clear_credentials', { issuer })
}
