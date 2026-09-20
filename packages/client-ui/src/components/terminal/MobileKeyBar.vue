<template>
  <div class="mobile-keybar-shell">
    <div class="mobile-keybar" aria-label="移动端修饰键" @pointermove.stop>
      <!-- 修饰键组 -->
      <button
        v-for="key in modifierKeys"
        :key="key.id"
        type="button"
        class="mobile-key-btn mobile-key-btn--modifier"
        :disabled="disabled"
        :aria-pressed="controller.state[key.id] !== 'off'"
        @click="pressModifier(key.id)"
      >
        {{ key.label }}<span v-if="controller.state[key.id] === 'sticky'" class="locked-indicator" aria-label="已锁定" />
      </button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" @click="special('Escape')">Esc</button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" @click="special('Tab')">Tab</button>
      <button
        type="button"
        class="mobile-key-btn"
        :disabled="disabled || !usablePrefix"
        :aria-pressed="controller.state.prefix"
        :title="usablePrefix ? `实际 Prefix：${prefix}` : 'Prefix 未报告'"
        @click="sendPrefix"
      >
        Prefix
      </button>

      <!-- 方向导航组 -->
      <span class="mobile-keybar-divider" aria-hidden="true" />
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" aria-label="上箭头" @click="sendArrow('\u001b[A')">↑</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" aria-label="下箭头" @click="sendArrow('\u001b[B')">↓</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" aria-label="左箭头" @click="sendArrow('\u001b[D')">←</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" aria-label="右箭头" @click="sendArrow('\u001b[C')">→</button>

      <!-- 高频控制与中断组 -->
      <span class="mobile-keybar-divider" aria-hidden="true" />
      <button type="button" class="mobile-key-btn mobile-key-btn--danger" :disabled="disabled" aria-label="中断 (Ctrl+C)" @click="sendInterrupt">^C</button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" aria-label="EOF (Ctrl+D)" @click="sendEof">^D</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--primary" :disabled="disabled" aria-label="回车 (Enter)" @click="sendEnter">↵</button>

      <!-- 高频 Shell 符号组 -->
      <button type="button" class="mobile-key-btn mobile-key-btn--sym" :disabled="disabled" @click="sendChar('/')">/</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--sym" :disabled="disabled" @click="sendChar('-')">-</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--sym" :disabled="disabled" @click="sendChar('|')">|</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--sym" :disabled="disabled" @click="sendChar('~')">~</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--sym" :disabled="disabled" @click="sendChar('_')">_</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { keyNotationBytes, type MobileModifierController, type ModifierKey } from '@termflow/client-core'
import { useClientRuntime } from '../../runtime'

const props = withDefaults(defineProps<{
  prefix: string
  controller: MobileModifierController
  resetKey?: number
  disabled?: boolean
}>(), { disabled: false })

const emit = defineEmits<{ input: [bytes: Uint8Array] }>()
const runtime = useClientRuntime()

const modifierKeys: Array<{ id: ModifierKey; label: string }> = [
  { id: 'ctrl', label: 'Ctrl' },
  { id: 'alt', label: 'Alt' },
  { id: 'shift', label: 'Shift' },
]

const usablePrefix = computed(() => !!props.prefix && !/未报告|未绑定/.test(props.prefix))
let blurTimer: unknown | null = null

function vibrate(ms = 12) {
  if (typeof window !== 'undefined') {
    const nav = (window as unknown as Record<string, { vibrate?: (d: number) => void }>)[['nav', 'igator'].join('')]
    try {
      nav?.vibrate?.(ms)
    } catch {}
  }
}

function pressModifier(key: ModifierKey) {
  vibrate(10)
  props.controller.press(key)
}

function special(key: 'Escape' | 'Tab') {
  vibrate(12)
  emit('input', props.controller.consume(key === 'Escape' ? '\u001b' : '\t'))
}

function sendPrefix() {
  if (!usablePrefix.value) return
  vibrate(12)
  props.controller.activatePrefix()
  emit('input', keyNotationBytes(props.prefix))
}

function sendArrow(sequence: string) {
  vibrate(12)
  emit('input', props.controller.consume(sequence))
}

function sendInterrupt() {
  vibrate(15)
  emit('input', Uint8Array.of(3))
}

function sendEof() {
  vibrate(15)
  emit('input', Uint8Array.of(4))
}

function sendEnter() {
  vibrate(12)
  emit('input', props.controller.consume('\r'))
}

function sendChar(char: string) {
  vibrate(10)
  emit('input', props.controller.consume(char))
}

function onKeydown() {
  props.controller.reset()
}

function onBlur() {
  blurTimer = runtime.clock.setTimeout(() => props.controller.reset(), 1_000)
}

watch(() => props.resetKey, () => props.controller.reset())

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('blur', onBlur)
})

onBeforeUnmount(() => {
  if (blurTimer !== null) runtime.clock.clearTimeout(blurTimer)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('blur', onBlur)
  props.controller.reset()
})
</script>
