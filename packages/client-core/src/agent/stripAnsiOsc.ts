//: Render-safety utility (M6b spec §6.2): strips ANSI/OSC escape sequences
//: (CSI, OSC, DCS) from agent text before it reaches the UI. Plain text is
//: preserved verbatim; an incomplete trailing sequence (a lone ESC, an
//: unterminated CSI, or an OSC/DCS without its terminator) is dropped, so
//: streaming deltas can be stripped independently without leaking partial
//: escape noise. A malformed CSI (a byte outside the ECMA-48 parameter/
//: intermediate/final classes) ends the sequence at the introducer so the
//: following bytes stay plain text. Stripping is not sanitization:
//: HTML-shaped text and URL-shaped text pass through untouched — the UI
//: renders them as inert text (never v-html, never linkified).

const ESC = 0x1b
const CSI_INTRO = 0x5b // '['
const OSC_INTRO = 0x5d // ']'
const DCS_INTRO = 0x50 // 'P'
const BEL = 0x07
const ST_FINAL = 0x5c // '\'

/**
 * Strip ANSI/OSC escape sequences from a string and return the remaining
 * plain text. CSI sequences (`ESC [` … final byte), OSC sequences
 * (`ESC ]` … BEL or `ESC \`), and DCS sequences (`ESC P` … `ESC \`) are
 * removed entirely. A trailing lone ESC or an unterminated sequence at the
 * end of the input is dropped so that a partially received streaming chunk
 * renders nothing instead of escape noise.
 */
export function stripAnsiOsc(input: string): string {
  let output = ''
  let i = 0
  const n = input.length
  while (i < n) {
    if (input.charCodeAt(i) !== ESC) {
      output += input.charAt(i)
      i += 1
      continue
    }
    const intro = i + 1 < n ? input.charCodeAt(i + 1) : -1
    if (intro === CSI_INTRO) {
      // CSI: parameter bytes 0x30-0x3F, intermediate bytes 0x20-0x2F, then
      // a final byte 0x40-0x7E. Reaching the end first (unterminated CSI)
      // drops the tail; a byte outside all three classes (a malformed CSI)
      // ends the sequence at the introducer so the following bytes stay
      // plain text.
      let j = i + 2
      while (j < n && input.charCodeAt(j) >= 0x30 && input.charCodeAt(j) <= 0x3f) j += 1
      while (j < n && input.charCodeAt(j) >= 0x20 && input.charCodeAt(j) <= 0x2f) j += 1
      if (j < n && input.charCodeAt(j) >= 0x40 && input.charCodeAt(j) <= 0x7e) j += 1
      i = j
      continue
    }
    if (intro === OSC_INTRO || intro === DCS_INTRO) {
      // OSC/DCS: everything up to BEL or ST (ESC \) is command content.
      // An ESC that is not part of an ST is payload, not a terminator.
      let j = i + 2
      while (j < n) {
        if (input.charCodeAt(j) === BEL) {
          j += 1
          break
        }
        if (input.charCodeAt(j) === ESC && j + 1 < n && input.charCodeAt(j + 1) === ST_FINAL) {
          j += 2
          break
        }
        j += 1
      }
      i = j
      continue
    }
    // Unknown introducer (or a trailing lone ESC): drop the ESC only and
    // keep scanning — the following byte stays plain text.
    i += 1
  }
  return output
}
