import { describe, expect, it } from 'vitest'
import { stripAnsiOsc } from './stripAnsiOsc'

const esc = '\u001b'
const bel = '\u0007'

describe('stripAnsiOsc', () => {
  it('strips CSI sequences and keeps surrounding text', () => {
    expect(stripAnsiOsc(`a${esc}[31mred${esc}[0m b`)).toBe('ared b')
    expect(stripAnsiOsc(`a ${esc}[31mred${esc}[0m b`)).toBe('a red b')
    expect(stripAnsiOsc(`${esc}[1m${esc}[31mbold red${esc}[0m plain`)).toBe('bold red plain')
    expect(stripAnsiOsc(`${esc}[38;2;255;0;0mcolored${esc}[0m`)).toBe('colored')
    expect(stripAnsiOsc(`${esc}[31m${esc}[0m`)).toBe('')
  })

  it('strips OSC sequences terminated by BEL or ST', () => {
    expect(stripAnsiOsc(`${esc}]0;title${bel}rest`)).toBe('rest')
    expect(stripAnsiOsc(`${esc}]0;title${esc}\\rest`)).toBe('rest')
  })

  it('strips DCS sequences terminated by ST', () => {
    expect(stripAnsiOsc(`${esc}P1;2|payload${esc}\\tail`)).toBe('tail')
  })

  it('strips hyperlink OSC wrappers (linkification threat sample)', () => {
    expect(stripAnsiOsc(`${esc}]8;;https://example.invalid${esc}\\link${esc}]8;;${esc}\\`)).toBe('link')
    expect(stripAnsiOsc(`${esc}]8;;javascript:alert(1)${bel}click${esc}]8;;${bel}`)).toBe('click')
  })

  it('drops incomplete trailing sequences at streaming chunk boundaries', () => {
    expect(stripAnsiOsc(`text${esc}`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}[`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}[31`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}[31;4`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}]0;unterminated`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}Ppayload`)).toBe('text')
    expect(stripAnsiOsc(`text${esc}]0;title${esc}`)).toBe('text')
  })

  it('leaves plain text untouched', () => {
    expect(stripAnsiOsc('')).toBe('')
    expect(stripAnsiOsc('no escape here')).toBe('no escape here')
    expect(stripAnsiOsc('中文 😀 \n\t')).toBe('中文 😀 \n\t')
    expect(stripAnsiOsc('[31m is not a sequence')).toBe('[31m is not a sequence')
    expect(stripAnsiOsc('a]b P c\\ d')).toBe('a]b P c\\ d')
  })

  it('keeps HTML and URL-shaped text as plain text (stripping is not sanitization)', () => {
    expect(stripAnsiOsc('<script>alert(1)</script>')).toBe('<script>alert(1)</script>')
    expect(stripAnsiOsc('javascript:alert(1)')).toBe('javascript:alert(1)')
    expect(stripAnsiOsc('see <img src=x> here')).toBe('see <img src=x> here')
  })

  it('drops only the ESC of unknown sequences, keeping the following byte', () => {
    expect(stripAnsiOsc(`a${esc}b`)).toBe('ab')
    expect(stripAnsiOsc(`${esc}(Bplain`)).toBe('(Bplain')
  })

  it('ends a malformed CSI at the introducer so following bytes stay plain text', () => {
    expect(stripAnsiOsc(`b${esc}[${bel} :>b`)).toBe(`b${bel} :>b`)
    expect(stripAnsiOsc(`x${esc}[😀y`)).toBe('x😀y')
  })

  it('treats an ESC inside an OSC payload as payload, terminating only on ST or BEL', () => {
    expect(stripAnsiOsc(`${esc}]0;a${esc}b${bel}after`)).toBe('after')
    expect(stripAnsiOsc(`${esc}]0;a${esc}b${esc}\\after`)).toBe('after')
  })
})
