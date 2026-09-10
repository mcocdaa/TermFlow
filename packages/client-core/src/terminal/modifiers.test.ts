import { describe, expect, it } from 'vitest'
import { MobileModifierController, keyNotationBytes } from './modifiers'

describe('mobile terminal modifiers', () => {
  it('applies one-shot modifiers and resets them after a key', () => {
    const modifiers = new MobileModifierController()
    modifiers.press('ctrl')
    expect([...modifiers.consume('c')]).toEqual([3])
    expect(modifiers.state.ctrl).toBe('off')
    modifiers.press('alt')
    expect([...modifiers.consume('x')]).toEqual([27, 120])
    modifiers.press('shift')
    expect(new TextDecoder().decode(modifiers.consume('a'))).toBe('A')
  })

  it('cycles through one-shot, sticky, and off', () => {
    const modifiers = new MobileModifierController()
    modifiers.press('ctrl')
    modifiers.press('ctrl')
    expect(modifiers.state.ctrl).toBe('sticky')
    modifiers.consume('c')
    expect(modifiers.state.ctrl).toBe('sticky')
    modifiers.press('ctrl')
    expect(modifiers.state.ctrl).toBe('off')
  })

  it('encodes only the server-reported key notation', () => {
    expect([...keyNotationBytes('C-a')]).toEqual([1])
    expect([...keyNotationBytes('M-x')]).toEqual([27, 120])
    expect([...keyNotationBytes('Escape')]).toEqual([27])
    expect([...keyNotationBytes('Tab')]).toEqual([9])
  })

  it('can use an injected observable state object without importing Vue', () => {
    const state = { ctrl: 'off', alt: 'off', shift: 'off', prefix: false } as const
    const mutable = { ...state }
    const modifiers = new MobileModifierController(mutable)
    modifiers.press('ctrl')
    expect(mutable.ctrl).toBe('once')
    modifiers.reset()
    expect(mutable).toEqual(state)
  })
})
