import { reactive } from 'vue'

export type ModifierMode = 'off' | 'once' | 'sticky'
export type ModifierKey = 'ctrl' | 'alt' | 'shift'

export function keyNotationBytes(notation: string): Uint8Array {
  const normalized = notation.trim()
  const control = /^(?:C-|Ctrl\+)(.)$/i.exec(normalized)
  if (control) return Uint8Array.of(control[1].toUpperCase().charCodeAt(0) & 31)
  const alt = /^(?:M-|Alt\+)(.)$/i.exec(normalized)
  if (alt) return Uint8Array.of(27, ...new TextEncoder().encode(alt[1]))
  if (/^(?:Esc|Escape)$/i.test(normalized)) return Uint8Array.of(27)
  if (/^Tab$/i.test(normalized)) return Uint8Array.of(9)
  return new TextEncoder().encode(normalized)
}

export class MobileModifierController {
  readonly state = reactive<{ ctrl: ModifierMode; alt: ModifierMode; shift: ModifierMode; prefix: boolean }>({ ctrl: 'off', alt: 'off', shift: 'off', prefix: false })
  press(key: ModifierKey) { this.state[key] = this.state[key] === 'off' ? 'once' : this.state[key] === 'once' ? 'sticky' : 'off' }
  activatePrefix() { this.state.prefix = true }
  consume(value: string): Uint8Array {
    let text = value
    if (this.state.shift !== 'off' && /^[a-z]$/.test(text)) text = text.toUpperCase()
    let bytes = new TextEncoder().encode(text)
    if (this.state.ctrl !== 'off' && text.length === 1) bytes = Uint8Array.of(text.toUpperCase().charCodeAt(0) & 31)
    if (this.state.alt !== 'off') bytes = Uint8Array.of(27, ...bytes)
    for (const key of ['ctrl', 'alt', 'shift'] as const) if (this.state[key] === 'once') this.state[key] = 'off'
    this.state.prefix = false
    return bytes
  }
  reset() { this.state.ctrl = 'off'; this.state.alt = 'off'; this.state.shift = 'off'; this.state.prefix = false }
}
