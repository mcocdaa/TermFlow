<template>
  <div class="mobile-keybar-shell">
    <div class="mobile-keybar" :aria-label="t('terminal.mobileKeybar.aria')" @pointermove.stop>
      <!-- Modifier keys -->
      <button
        v-for="key in modifierKeys"
        :key="key.id"
        type="button"
        class="mobile-key-btn mobile-key-btn--modifier"
        :disabled="disabled"
        :aria-pressed="controller.state[key.id] !== 'off'"
        @click="pressModifier(key.id)"
      >
        {{ key.label }}<span v-if="controller.state[key.id] === 'sticky'" class="locked-indicator" :aria-label="t('terminal.mobileKeybar.locked')" />
      </button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" @click="special('Escape')">Esc</button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" @click="special('Tab')">Tab</button>
      <button
        type="button"
        class="mobile-key-btn"
        :disabled="disabled || !usablePrefix"
        :aria-pressed="controller.state.prefix"
        :title="usablePrefix ? t('terminal.mobileKeybar.prefixReported', { prefix }) : t('terminal.mobileKeybar.prefixUnreported')"
        @click="sendPrefix"
      >
        <span class="mobile-keybar__label">{{ t('terminal.mobileKeybar.prefix') }}</span>
      </button>

      <!-- Navigation keys -->
      <span class="mobile-keybar-divider" aria-hidden="true" />
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.up')" @click="sendArrow('\u001b[A')">↑</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.down')" @click="sendArrow('\u001b[B')">↓</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.left')" @click="sendArrow('\u001b[D')">←</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--nav" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.right')" @click="sendArrow('\u001b[C')">→</button>

      <!-- Control & interrupt keys -->
      <span class="mobile-keybar-divider" aria-hidden="true" />
      <button type="button" class="mobile-key-btn mobile-key-btn--danger" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.interrupt')" @click="sendInterrupt">^C</button>
      <button type="button" class="mobile-key-btn" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.eof')" @click="sendEof">^D</button>
      <button type="button" class="mobile-key-btn mobile-key-btn--primary" :disabled="disabled" :aria-label="t('terminal.mobileKeybar.enter')" @click="sendEnter">↵</button>

      <!-- Shell symbol keys -->
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
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'

const { t } = useI18n()
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

const usablePrefix = computed(() => !!props.prefix && props.prefix !== t('terminal.unreportedPrefix') && props.prefix !== t('terminal.tmux.unbound') && !/\u672a\u62a5\u544a|\u672a\u7ed1\u5b9a|Unreported|Unbound/i.test(props.prefix))
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
