import { computed, inject, ref, type Ref } from 'vue'
import { locale, translateDynamicText } from '../i18n'
import type { ClockPort } from '../runtime'
import { bottomToastKey } from '../runtimeKey'

export type BottomToastTone = 'success' | 'error'

export interface BottomToastMessage {
  readonly text: string
  readonly tone: BottomToastTone
}

export interface BottomToastInput {
  readonly text: string | (() => string)
  readonly tone: BottomToastTone
}

export interface BottomToastController {
  readonly current: Readonly<Ref<BottomToastMessage | null>>
  show(message: BottomToastInput): void
  clear(): void
}

export function createBottomToast(clock: ClockPort): BottomToastController {
  const rawCurrent = ref<BottomToastInput | null>(null)
  const current = computed<BottomToastMessage | null>(() => {
    void locale.value
    if (rawCurrent.value === null) return null
    const rawText = typeof rawCurrent.value.text === 'function' ? rawCurrent.value.text() : rawCurrent.value.text
    return {
      text: translateDynamicText(rawText),
      tone: rawCurrent.value.tone,
    }
  })
  let timer: unknown | null = null
  function clear() {
    if (timer !== null) clock.clearTimeout(timer)
    timer = null
    rawCurrent.value = null
  }
  function show(message: BottomToastInput) {
    clear()
    rawCurrent.value = message
    timer = clock.setTimeout(() => {
      rawCurrent.value = null
      timer = null
    }, 3_000)
  }
  return { current, show, clear }
}

export function useBottomToast(): BottomToastController {
  const toast = inject(bottomToastKey, undefined)
  if (toast === undefined) throw new Error('TermFlow bottom toast is not installed.')
  return toast
}
