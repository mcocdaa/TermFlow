import { inject, ref, type Ref } from 'vue'
import type { ClockPort } from '../runtime'
import { bottomToastKey } from '../runtimeKey'

export type BottomToastTone = 'success' | 'error'

export interface BottomToastMessage {
  readonly text: string
  readonly tone: BottomToastTone
}

export interface BottomToastController {
  readonly current: Readonly<Ref<BottomToastMessage | null>>
  show(message: BottomToastMessage): void
  clear(): void
}

export function createBottomToast(clock: ClockPort): BottomToastController {
  const current = ref<BottomToastMessage | null>(null)
  let timer: unknown | null = null
  function clear() {
    if (timer !== null) clock.clearTimeout(timer)
    timer = null
    current.value = null
  }
  function show(message: BottomToastMessage) {
    clear()
    current.value = message
    timer = clock.setTimeout(() => {
      current.value = null
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
