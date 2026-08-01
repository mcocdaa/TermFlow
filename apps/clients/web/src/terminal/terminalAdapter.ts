import { Terminal } from '@xterm/xterm'

export interface TerminalAdapter {
  write(bytes: Uint8Array): void
  resize(cols: number, rows: number): void
  reset(): void
  focus(): void
  refreshTheme(): void
  setInputEnabled(enabled: boolean): void
  measureCell(): { width: number; height: number } | null
  canClientPan(): boolean
  dispose(): void
}
export type TerminalAdapterFactory = (host: HTMLElement, size: { rows: number; cols: number }, onInput: (value: string | Uint8Array) => void) => TerminalAdapter

function semanticTheme(host: HTMLElement) {
  const style = getComputedStyle(host)
  return {
    background: style.getPropertyValue('--color-terminal').trim(),
    foreground: style.getPropertyValue('--color-terminal-foreground').trim(),
    cursor: style.getPropertyValue('--color-accent').trim(),
    cursorAccent: style.getPropertyValue('--color-accent-contrast').trim(),
    selectionBackground: style.getPropertyValue('--color-terminal-selection').trim(),
    black: style.getPropertyValue('--terminal-black').trim(),
    red: style.getPropertyValue('--terminal-red').trim(),
    green: style.getPropertyValue('--terminal-green').trim(),
    yellow: style.getPropertyValue('--terminal-yellow').trim(),
    blue: style.getPropertyValue('--terminal-blue').trim(),
    magenta: style.getPropertyValue('--terminal-magenta').trim(),
    cyan: style.getPropertyValue('--terminal-cyan').trim(),
    white: style.getPropertyValue('--terminal-white').trim(),
    brightBlack: style.getPropertyValue('--terminal-bright-black').trim(),
    brightRed: style.getPropertyValue('--terminal-bright-red').trim(),
    brightGreen: style.getPropertyValue('--terminal-bright-green').trim(),
    brightYellow: style.getPropertyValue('--terminal-bright-yellow').trim(),
    brightBlue: style.getPropertyValue('--terminal-bright-blue').trim(),
    brightMagenta: style.getPropertyValue('--terminal-bright-magenta').trim(),
    brightCyan: style.getPropertyValue('--terminal-bright-cyan').trim(),
    brightWhite: style.getPropertyValue('--terminal-bright-white').trim(),
  }
}

export const createXtermAdapter: TerminalAdapterFactory = (host, size, onInput) => {
  const style = getComputedStyle(host)
  const terminal = new Terminal({
    rows: size.rows,
    cols: size.cols,
    allowTransparency: false,
    cursorBlink: true,
    disableStdin: true,
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
    refreshTheme: () => { terminal.options.theme = semanticTheme(host) },
    setInputEnabled: (enabled) => { terminal.options.disableStdin = !enabled },
    measureCell: () => {
      const screen = terminal.element?.querySelector<HTMLElement>('.xterm-screen')
      if (!screen || terminal.cols <= 0 || terminal.rows <= 0) return null
      const style = getComputedStyle(screen)
      const width = Number.parseFloat(style.width)
      const height = Number.parseFloat(style.height)
      if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
      return { width: width / terminal.cols, height: height / terminal.rows }
    },
    canClientPan: () => !terminal.hasSelection() && terminal.modes.mouseTrackingMode === 'none',
    dispose: () => { dataDisposable.dispose(); binaryDisposable.dispose(); terminal.dispose() },
  }
}
