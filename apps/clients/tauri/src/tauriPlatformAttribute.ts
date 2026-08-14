export function setTauriPlatformAttribute(root: HTMLElement, currentPlatform: string): void {
  if (currentPlatform === 'android') root.dataset.tauriPlatform = 'android'
  else delete root.dataset.tauriPlatform
}
