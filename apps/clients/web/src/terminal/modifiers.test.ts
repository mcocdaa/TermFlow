import { describe, expect, it } from 'vitest'
import { MobileModifierController, keyNotationBytes } from './modifiers'

describe('mobile terminal modifiers', () => {
  it('applies one-shot Ctrl/Alt/Shift and resets after a key', () => {
    const modifiers = new MobileModifierController()
    modifiers.press('ctrl')
    expect([...modifiers.consume('c')]).toEqual([3])
    expect(modifiers.state.ctrl).toBe('off')
    modifiers.press('alt')
    expect([...modifiers.consume('x')]).toEqual([27, 120])
    modifiers.press('shift')
    expect(new TextDecoder().decode(modifiers.consume('a'))).toBe('A')
  })

  it('cycles a modifier through one-shot, sticky, and off', () => {
    const modifiers = new MobileModifierController()
    modifiers.press('ctrl')
    modifiers.press('ctrl')
    expect(modifiers.state.ctrl).toBe('sticky')
    modifiers.consume('c')
    expect(modifiers.state.ctrl).toBe('sticky')
    modifiers.press('ctrl')
    expect(modifiers.state.ctrl).toBe('off')
  })

  it('encodes the server-reported prefix and never assumes another binding', () => {
    expect([...keyNotationBytes('C-a')]).toEqual([1])
    expect([...keyNotationBytes('M-x')]).toEqual([27, 120])
    expect([...keyNotationBytes('C-a')]).not.toEqual([2])
  })

  it('resets every modifier on replacement, timeout, or route leave', () => {
    const modifiers = new MobileModifierController()
    modifiers.press('ctrl'); modifiers.press('alt'); modifiers.activatePrefix()
    modifiers.reset()
    expect(modifiers.state).toEqual({ ctrl: 'off', alt: 'off', shift: 'off', prefix: false })
  })
})
