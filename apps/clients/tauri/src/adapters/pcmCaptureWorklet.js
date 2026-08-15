/**
 * AudioWorklet capture processor for the Tauri mobile PCM/WAV path
 * (M7b spec §4.3.2). Loaded via `audioWorklet.addModule` from a Vite-bundled
 * same-origin asset (CSP `script-src 'self'`, spec §4.10), so this file must
 * stay dependency-free and plain JavaScript: Vite emits it as-is (it is not
 * transpiled through the TS pipeline).
 *
 * The processor mixes the incoming channels down to mono and posts each
 * render quantum to the main thread as a transferred Float32Array. The
 * AudioContext is created at 16 kHz (`pcmWavEncoder.ts` PCM_WAV_SAMPLE_RATE),
 * so no resampling happens here. The output channel is left silent: the
 * recorder connects this node to `destination` only to keep the graph
 * pulling, and silence is what the user must hear while recording.
 *
 * Keep the processor name in sync with `PCM_CAPTURE_PROCESSOR_NAME` in
 * `tauriVoiceRecorder.ts`.
 */

const PCM_CAPTURE_PROCESSOR_NAME = 'termflow-pcm-capture'

class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0]
    if (input !== undefined && input.length > 0) {
      const frameCount = input[0].length
      if (frameCount > 0) {
        let mono
        if (input.length === 1) {
          mono = new Float32Array(input[0])
        } else {
          mono = new Float32Array(frameCount)
          const weight = 1 / input.length
          for (const channel of input) {
            for (let i = 0; i < frameCount; i++) {
              mono[i] += channel[i] * weight
            }
          }
        }
        this.port.postMessage(mono, [mono.buffer])
      }
    }
    return true
  }
}

registerProcessor(PCM_CAPTURE_PROCESSOR_NAME, PcmCaptureProcessor)
