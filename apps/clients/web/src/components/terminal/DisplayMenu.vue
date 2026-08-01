<template>
  <div class="titlebar-menu">
    <button ref="trigger" data-action="toggle-display-menu" class="titlebar-button" type="button" aria-haspopup="menu" :aria-expanded="open" @click="toggleMenu">显示 <span aria-hidden="true">▾</span></button>
    <div v-if="open" class="floating-menu display-menu" role="menu" aria-label="终端显示比例" @keydown.esc.prevent="closeAndFocus" @keydown.down.prevent="moveFocus(1)" @keydown.up.prevent="moveFocus(-1)">
      <button v-for="choice in choices" ref="choiceButtons" :key="choice.id" type="button" role="menuitemradio" :aria-checked="modelValue === choice.id" @click="select(choice.id)">
        <span aria-hidden="true">{{ modelValue === choice.id ? '●' : '○' }}</span> {{ choice.label }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import type { DisplayMode } from '../../terminal/viewport'
const props = defineProps<{ modelValue: DisplayMode }>()
const emit = defineEmits<{ 'update:modelValue': [mode: DisplayMode] }>()
const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
const choiceButtons = ref<HTMLButtonElement[]>([])
const choices: Array<{ id: DisplayMode; label: string }> = [
  { id: 'scale-50', label: '50%' }, { id: 'scale-75', label: '75%' }, { id: 'font-100', label: '100% 实际字号' }, { id: 'fit', label: '适应窗口' },
]
async function toggleMenu() {
  open.value = !open.value
  if (!open.value) return
  await nextTick()
  choiceButtons.value[choices.findIndex((choice) => choice.id === props.modelValue)]?.focus()
}
async function select(mode: DisplayMode) { emit('update:modelValue', mode); open.value = false; await nextTick(); trigger.value?.focus() }
function moveFocus(offset: number) {
  const current = choiceButtons.value.findIndex((button) => button === document.activeElement)
  choiceButtons.value[(current + offset + choiceButtons.value.length) % choiceButtons.value.length]?.focus()
}
async function closeAndFocus() { open.value = false; await nextTick(); trigger.value?.focus() }
</script>
