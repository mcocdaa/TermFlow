import { Terminal } from '@xterm/xterm'

export interface TerminalAdapter {
  write(bytes: Uint8Array): void
  resize(cols: number, rows: number): void
  reset(): void
  focus(): void
  canClientPan(): boolean
  dispose(): void
}
export type TerminalAdapterFactory = (host: HTMLElement, size: { rows: number; cols: number }, onInput: (value: string | Uint8Array) => void) => TerminalAdapter

function semanticTheme(host: HTMLElement) {
  const style = getComputedStyle(host)
  return {
    background: style.getPropertyValue('--color-terminal').trim(),
    foreground: style.getPropertyValue('--color-text-primary').trim(),
    cursor: style.getPropertyValue('--color-accent').trim(),
    cursorAccent: style.getPropertyValue('--color-accent-contrast').trim(),
    selectionBackground: style.getPropertyValue('--color-elevated').trim(),
  }
}

export const createXtermAdapter: TerminalAdapterFactory = (host, size, onInput) => {
  const style = getComputedStyle(host)
  const terminal = new Terminal({
    rows: size.rows,
    cols: size.cols,
    allowTransparency: false,
    cursorBlink: true,
    scrollback: 5_000,
    fontFamily: style.getPropertyValue('--font-mono').trim(),
    fontSize: 14,
    theme: semanticTheme(host),
  })
  terminal.open(host)
  const dataDisposable = terminal.onData(onInput)
  const binaryDisposable = terminal.onBinary((value) => onInput(Uint8Array.from(value, (character) => character.charCodeAt(0))))
  return {
    write: (bytes) => terminal.write(bytes),
    resize: (cols, rows) => terminal.resize(cols, rows),
    reset: () => terminal.reset(),
    focus: () => terminal.focus(),
    canClientPan: () => !terminal.hasSelection() && terminal.modes.mouseTrackingMode === 'none',
    dispose: () => { dataDisposable.dispose(); binaryDisposable.dispose(); terminal.dispose() },
  }
}
