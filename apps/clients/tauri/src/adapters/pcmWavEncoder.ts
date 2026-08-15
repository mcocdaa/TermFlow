/**
 * Pure-function 16 kHz mono 16-bit PCM WAV encoder (M7b spec §4.3.3).
 *
 * Fixed output layout, all little-endian:
 *   RIFF chunk (12 bytes): "RIFF" | chunkSize (= 36 + dataBytes) | "WAVE"
 *   fmt chunk (24 bytes):  "fmt " | 16 | audioFormat=1 (PCM) | channels=1 |
 *                          sampleRate=16000 | byteRate=32000 |
 *                          blockAlign=2 | bitsPerSample=16
 *   data chunk (8 bytes):  "data" | dataBytes
 *   samples: int16 LE
 *
 * Every field matches what the B side `_wav_sample_rate_duration` parser
 * accepts (fmt chunk ≥ 16 bytes, PCM, 8-96 kHz sample rate, byte_rate > 0,
 * M7b spec §3.1/§4.3.3). 16 kHz mono is 1.92 MB/min, so the 240 s capture
 * ceiling (§4.4) yields 7.68 MB — provably below the 10 MiB upload bound.
 */

export const PCM_WAV_SAMPLE_RATE = 16000
export const PCM_WAV_CHANNELS = 1
export const PCM_WAV_BITS_PER_SAMPLE = 16
export const PCM_WAV_BYTES_PER_SAMPLE = PCM_WAV_BITS_PER_SAMPLE / 8
export const PCM_WAV_HEADER_BYTES = 44
export const PCM_WAV_MIME_TYPE = 'audio/wav'

/** +32767 ceiling for positive samples. */
const MAX_POSITIVE_SAMPLE = 0x7fff
/** -32768 floor for negative samples (full negative scale). */
const MAX_NEGATIVE_SAMPLE = 0x8000

/**
 * Encode normalized (-1..1) mono samples as a 16 kHz 16-bit PCM WAV file.
 * Out-of-range values are clamped; the output is deterministic and has no
 * side effects (pure function, byte-level unit tested).
 */
export function encodePcmToWav(samples: Float32Array): ArrayBuffer {
  const dataBytes = samples.length * PCM_WAV_BYTES_PER_SAMPLE
  const buffer = new ArrayBuffer(PCM_WAV_HEADER_BYTES + dataBytes)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // audio format: PCM
  view.setUint16(22, PCM_WAV_CHANNELS, true)
  view.setUint32(24, PCM_WAV_SAMPLE_RATE, true)
  view.setUint32(28, PCM_WAV_SAMPLE_RATE * PCM_WAV_CHANNELS * PCM_WAV_BYTES_PER_SAMPLE, true) // byte rate
  view.setUint16(32, PCM_WAV_CHANNELS * PCM_WAV_BYTES_PER_SAMPLE, true) // block align
  view.setUint16(34, PCM_WAV_BITS_PER_SAMPLE, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataBytes, true)

  for (let i = 0; i < samples.length; i++) {
    const normalized = Math.max(-1, Math.min(1, samples[i] ?? 0))
    view.setInt16(PCM_WAV_HEADER_BYTES + i * PCM_WAV_BYTES_PER_SAMPLE, encodeSample(normalized), true)
  }
  return buffer
}

function encodeSample(normalized: number): number {
  // Negative samples use the full 32768 scale so -1.0 maps to -32768;
  // positive samples stop at +32767 (the standard asymmetric convention).
  return Math.round(normalized < 0 ? normalized * MAX_NEGATIVE_SAMPLE : normalized * MAX_POSITIVE_SAMPLE)
}

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) {
    view.setUint8(offset + i, text.charCodeAt(i))
  }
}
