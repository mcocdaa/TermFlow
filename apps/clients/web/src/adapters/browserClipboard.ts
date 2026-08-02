import type { ClipboardPort } from '@termflow/client-ui'

type BrowserClipboard = Pick<Clipboard, 'writeText'>

export function createBrowserClipboard(clipboard: BrowserClipboard = globalThis.navigator.clipboard): ClipboardPort {
  return { writeText: (text) => clipboard.writeText(text) }
}
