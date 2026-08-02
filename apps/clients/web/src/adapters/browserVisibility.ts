import type { VisibilityPort } from '@termflow/client-ui'

export interface BrowserVisibilitySource {
  isHidden(): boolean
  subscribe(listener: () => void): void
  unsubscribe(listener: () => void): void
}

function browserDocumentSource(): BrowserVisibilitySource {
  return {
    isHidden: () => document.visibilityState === 'hidden',
    subscribe: (listener) => document.addEventListener('visibilitychange', listener),
    unsubscribe: (listener) => document.removeEventListener('visibilitychange', listener),
  }
}

export function createBrowserVisibility(source: BrowserVisibilitySource = browserDocumentSource()): VisibilityPort {
  return {
    isHidden: () => source.isHidden(),
    subscribe(listener) {
      source.subscribe(listener)
      return () => source.unsubscribe(listener)
    },
  }
}
