import { describe, expect, it } from 'vitest'
import {
  encodePcmToWav,
  PCM_WAV_BITS_PER_SAMPLE,
  PCM_WAV_BYTES_PER_SAMPLE,
  PCM_WAV_CHANNELS,
  PCM_WAV_HEADER_BYTES,
  PCM_WAV_MIME_TYPE,
  PCM_WAV_SAMPLE_RATE,
} from './pcmWavEncoder'

function headerBytes(buffer: ArrayBuffer): Uint8Array {
  return new Uint8Array(buffer).slice(0, PCM_WAV_HEADER_BYTES)
}

describe('encodePcmToWav (§4.3.3)', () => {
  it('writes the exact 44-byte RIFF/fmt/data header, little-endian', () => {
    const buffer = encodePcmToWav(new Float32Array([1, -1, 0.5]))

    // 3 samples × 16 bits = 6 data bytes; RIFF size = 36 + 6.
    expect(headerBytes(buffer)).toEqual(
      new Uint8Array([
        0x52, 0x49, 0x46, 0x46, // "RIFF"
        0x2a, 0x00, 0x00, 0x00, // chunk size 42 (36 + 6), LE
        0x57, 0x41, 0x56, 0x45, // "WAVE"
        0x66, 0x6d, 0x74, 0x20, // "fmt "
        0x10, 0x00, 0x00, 0x00, // fmt chunk size 16, LE
        0x01, 0x00, // audio format PCM
        0x01, 0x00, // channels 1
        0x80, 0x3e, 0x00, 0x00, // sample rate 16000, LE
        0x00, 0x7d, 0x00, 0x00, // byte rate 32000, LE
        0x02, 0x00, // block align 2
        0x10, 0x00, // bits per sample 16
        0x64, 0x61, 0x74, 0x61, // "data"
        0x06, 0x00, 0x00, 0x00, // data size 6, LE
      ]),
    )
  })

  it('encodes normalized samples as 16-bit PCM with the standard ±full-scale convention', () => {
    const buffer = encodePcmToWav(new Float32Array([1, -1, 0.5, -0.5, 0]))
    const view = new DataView(buffer)

    expect(view.getInt16(44, true)).toBe(0x7fff) // 1.0 → +32767
    expect(view.getInt16(46, true)).toBe(-0x8000) // -1.0 → -32768
    expect(view.getInt16(48, true)).toBe(16384) // 0.5 → +16384
    expect(view.getInt16(50, true)).toBe(-16384) // -0.5 → -16384
    expect(view.getInt16(52, true)).toBe(0)
  })

  it('clamps out-of-range samples instead of wrapping', () => {
    const buffer = encodePcmToWav(new Float32Array([1.5, -1.5]))
    const view = new DataView(buffer)

    expect(view.getInt16(44, true)).toBe(0x7fff)
    expect(view.getInt16(46, true)).toBe(-0x8000)
  })

  it('produces an empty WAV for empty input', () => {
    const buffer = encodePcmToWav(new Float32Array())
    const view = new DataView(buffer)

    expect(buffer.byteLength).toBe(PCM_WAV_HEADER_BYTES)
    expect(view.getUint32(4, true)).toBe(36)
    expect(view.getUint32(40, true)).toBe(0)
  })

  it('sizes the output as 44 + 2 bytes per sample (240s ceiling math)', () => {
    // 240 seconds of 16 kHz mono PCM = 3,840,000 samples = 7,680,000 bytes,
    // i.e. 7.32 MiB — the spec's proof that the client ceiling stays under
    // the B side 10 MiB bound (M7b spec §4.3.3/§4.4).
    const sampleCount = PCM_WAV_SAMPLE_RATE * 240
    const buffer = encodePcmToWav(new Float32Array(sampleCount))
    const view = new DataView(buffer)

    expect(buffer.byteLength).toBe(PCM_WAV_HEADER_BYTES + sampleCount * PCM_WAV_BYTES_PER_SAMPLE)
    expect(buffer.byteLength).toBeLessThan(10 * 1024 * 1024)
    expect(view.getUint32(4, true)).toBe(36 + sampleCount * PCM_WAV_BYTES_PER_SAMPLE)
    expect(view.getUint32(40, true)).toBe(sampleCount * PCM_WAV_BYTES_PER_SAMPLE)
  })

  it('declares the constants the B side WAV parser expects (§4.3.3)', () => {
    expect(PCM_WAV_SAMPLE_RATE).toBe(16000)
    expect(PCM_WAV_CHANNELS).toBe(1)
    expect(PCM_WAV_BITS_PER_SAMPLE).toBe(16)
    expect(PCM_WAV_HEADER_BYTES).toBe(44)
    expect(PCM_WAV_MIME_TYPE).toBe('audio/wav')
  })
})
