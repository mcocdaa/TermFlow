import { Terminal } from '@xterm/xterm'

const BASE_FONT_SIZE = 14

export interface TerminalCellMetrics {
  width: number
  height: number
}

export type TerminalMouseEventType = 'mousedown' | 'mousemove' | 'mouseup'

export interface TerminalMouseDispatch {
  type: TerminalMouseEventType
  clientX: number
  clientY: number
  buttons: 0 | 1
  button: 0
  detail?: 1 | 2
  forceSelection?: boolean
}

const MAC_PLATFORMS = new Set(['Macintosh', 'MacIntel', 'MacPPC', 'Mac68K'])

export function forceSelectionModifiers(platform: string, mouseTrackingActive: boolean): Pick<MouseEventInit, 'altKey' | 'shiftKey'> {
  if (!mouseTrackingActive) return {}
  return MAC_PLATFORMS.has(platform) ? { altKey: true } : { shiftKey: true }
}

export function visualFontSize(baseFontSize: number, scale: number): number {
  const normalizedScale = Number.isFinite(scale) && scale > 0 ? scale : 1
  return baseFontSize * normalizedScale
}

export function nativeWheelAvailable(hasSelection: boolean, mouseTrackingMode: string): boolean {
  return !hasSelection && mouseTrackingMode === 'none'
}

export interface TerminalAdapter {
  write(bytes: Uint8Array): void
  resize(cols: number, rows: number): void
  reset(): void
  focus(): void
  refreshTheme(): void
  setInputEnabled(enabled: boolean): void
  measureCell(): TerminalCellMetrics | null
  setVisualScale(scale: number): TerminalCellMetrics | null
  canClientPan(): boolean
  canNativeWheel(): boolean
  dispatchMouse(event: TerminalMouseDispatch): void
  dispose(): void
}
export type TerminalAdapterFactory = (host: HTMLElement, size: { rows: number; cols: number }, onInput: (value: string | Uint8Array) => void, platform: string) => TerminalAdapter

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

export const createXtermAdapter: TerminalAdapterFactory = (host, size, onInput, platform) => {
  const style = getComputedStyle(host)
  const terminal = new Terminal({
    rows: size.rows,
    cols: size.cols,
    allowTransparency: false,
    cursorBlink: true,
    disableStdin: true,
    macOptionClickForcesSelection: true,
    scrollback: 5_000,
    fontFamily: style.getPropertyValue('--font-mono').trim(),
    fontSize: BASE_FONT_SIZE,
    theme: semanticTheme(host),
  })
  terminal.open(host)
  const dataDisposable = terminal.onData(onInput)
  const binaryDisposable = terminal.onBinary((value) => onInput(Uint8Array.from(value, (character) => character.charCodeAt(0))))
  const measureCell = (): TerminalCellMetrics | null => {
    const screen = terminal.element?.querySelector<HTMLElement>('.xterm-screen')
    if (!screen || terminal.cols <= 0 || terminal.rows <= 0) return null
    const style = getComputedStyle(screen)
    const width = Number.parseFloat(style.width)
    const height = Number.parseFloat(style.height)
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
    return { width: width / terminal.cols, height: height / terminal.rows }
  }
  return {
    write: (bytes) => terminal.write(bytes),
    resize: (cols, rows) => terminal.resize(cols, rows),
    reset: () => terminal.reset(),
    focus: () => terminal.focus(),
    refreshTheme: () => { terminal.options.theme = semanticTheme(host) },
    setInputEnabled: (enabled) => { terminal.options.disableStdin = !enabled },
    measureCell,
    setVisualScale: (scale) => {
      terminal.options.fontSize = visualFontSize(BASE_FONT_SIZE, scale)
      return measureCell()
    },
    canClientPan: () => !terminal.hasSelection(),
    canNativeWheel: () => nativeWheelAvailable(
      terminal.hasSelection(),
      terminal.modes.mouseTrackingMode,
    ),
    dispatchMouse: (event) => {
      const element = terminal.element
      if (!element) return
      const mouseTrackingActive = terminal.modes.mouseTrackingMode !== 'none'
      const modifiers = event.forceSelection
        ? forceSelectionModifiers(platform, mouseTrackingActive)
        : {}
      const target: EventTarget = event.type === 'mousedown' ? element : element.ownerDocument
      target.dispatchEvent(new MouseEvent(event.type, {
        bubbles: true,
        cancelable: true,
        clientX: event.clientX,
        clientY: event.clientY,
        buttons: event.buttons,
        button: event.button,
        detail: event.detail ?? 1,
        ...modifiers,
      }))
    },
    dispose: () => { dataDisposable.dispose(); binaryDisposable.dispose(); terminal.dispose() },
  }
}
