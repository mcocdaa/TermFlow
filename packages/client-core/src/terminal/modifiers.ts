export type ModifierMode = 'off' | 'once' | 'sticky'
export type ModifierKey = 'ctrl' | 'alt' | 'shift'

export interface ModifierState {
  ctrl: ModifierMode
  alt: ModifierMode
  shift: ModifierMode
  prefix: boolean
}

export function createModifierState(): ModifierState {
  return { ctrl: 'off', alt: 'off', shift: 'off', prefix: false }
}

export function keyNotationBytes(notation: string): Uint8Array {
  const normalized = notation.trim()
  const control = /^(?:C-|Ctrl\+)(.)$/i.exec(normalized)
  if (control?.[1]) return Uint8Array.of(control[1].toUpperCase().charCodeAt(0) & 31)
  const alt = /^(?:M-|Alt\+)(.)$/i.exec(normalized)
  if (alt?.[1]) return Uint8Array.of(27, ...new TextEncoder().encode(alt[1]))
  if (/^(?:Esc|Escape)$/i.test(normalized)) return Uint8Array.of(27)
  if (/^Tab$/i.test(normalized)) return Uint8Array.of(9)
  return new TextEncoder().encode(normalized)
}

export class MobileModifierController {
  constructor(readonly state: ModifierState = createModifierState()) {}

  press(key: ModifierKey): void {
    this.state[key] = this.state[key] === 'off' ? 'once' : this.state[key] === 'once' ? 'sticky' : 'off'
  }

  activatePrefix(): void { this.state.prefix = true }

  consume(value: string): Uint8Array {
    let text = value
    if (this.state.shift !== 'off' && /^[a-z]$/.test(text)) text = text.toUpperCase()
    let bytes = new TextEncoder().encode(text)
    if (this.state.ctrl !== 'off' && text.length === 1) bytes = Uint8Array.of(text.toUpperCase().charCodeAt(0) & 31)
    if (this.state.alt !== 'off') bytes = Uint8Array.of(27, ...bytes)
    for (const key of ['ctrl', 'alt', 'shift'] as const) {
      if (this.state[key] === 'once') this.state[key] = 'off'
    }
    this.state.prefix = false
    return bytes
  }

  reset(): void {
    this.state.ctrl = 'off'
    this.state.alt = 'off'
    this.state.shift = 'off'
    this.state.prefix = false
  }
}
