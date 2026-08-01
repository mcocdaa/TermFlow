<template>
  <div class="mobile-keybar" aria-label="移动端修饰键">
    <button v-for="key in modifierKeys" :key="key.id" type="button" :disabled="disabled" :aria-pressed="controller.state[key.id] !== 'off'" @click="controller.press(key.id)">{{ key.label }}<span v-if="controller.state[key.id] === 'sticky'" aria-label="已锁定"> •</span></button>
    <button type="button" :disabled="disabled" @click="special('Escape')">Esc</button>
    <button type="button" :disabled="disabled" @click="special('Tab')">Tab</button>
    <button type="button" :disabled="disabled || !usablePrefix" :aria-pressed="controller.state.prefix" :title="usablePrefix ? `实际 Prefix：${prefix}` : 'Prefix 未报告'" @click="sendPrefix">Prefix</button>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { keyNotationBytes, type MobileModifierController, type ModifierKey } from '../../terminal/modifiers'
const props = withDefaults(defineProps<{ prefix: string; controller: MobileModifierController; resetKey?: number; disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ input: [bytes: Uint8Array] }>()
const modifierKeys: Array<{ id: ModifierKey; label: string }> = [{ id: 'ctrl', label: 'Ctrl' }, { id: 'alt', label: 'Alt' }, { id: 'shift', label: 'Shift' }]
const usablePrefix = computed(() => !!props.prefix && !/未报告|未绑定/.test(props.prefix))
let blurTimer: ReturnType<typeof setTimeout> | null = null
function special(key: 'Escape' | 'Tab') { emit('input', props.controller.consume(key === 'Escape' ? '\u001b' : '\t')) }
function sendPrefix() { if (!usablePrefix.value) return; props.controller.activatePrefix(); emit('input', keyNotationBytes(props.prefix)) }
function onKeydown() { props.controller.reset() }
function onBlur() { blurTimer = setTimeout(() => props.controller.reset(), 1_000) }
watch(() => props.resetKey, () => props.controller.reset())
onMounted(() => { window.addEventListener('keydown', onKeydown); window.addEventListener('blur', onBlur) })
onBeforeUnmount(() => { if (blurTimer !== null) clearTimeout(blurTimer); window.removeEventListener('keydown', onKeydown); window.removeEventListener('blur', onBlur); props.controller.reset() })
</script>
