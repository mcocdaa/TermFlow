<template>
  <div class="titlebar-menu">
    <button data-action="toggle-display-menu" class="titlebar-button" type="button" aria-haspopup="menu" :aria-expanded="open" @click="open = !open">显示 <span aria-hidden="true">▾</span></button>
    <div v-if="open" class="floating-menu display-menu" role="menu" aria-label="终端显示比例" @keydown.esc.prevent="closeAndFocus">
      <button v-for="choice in choices" :key="choice.id" type="button" role="menuitemradio" :aria-checked="modelValue === choice.id" @click="select(choice.id)">
        <span aria-hidden="true">{{ modelValue === choice.id ? '●' : '○' }}</span> {{ choice.label }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { DisplayMode } from '../../terminal/viewport'
const props = defineProps<{ modelValue: DisplayMode }>()
const emit = defineEmits<{ 'update:modelValue': [mode: DisplayMode] }>()
const open = ref(false)
const choices: Array<{ id: DisplayMode; label: string }> = [
  { id: 'scale-50', label: '50%' }, { id: 'scale-75', label: '75%' }, { id: 'font-100', label: '100% 实际字号' }, { id: 'fit', label: '适应窗口' },
]
function select(mode: DisplayMode) { emit('update:modelValue', mode); open.value = false }
function closeAndFocus(event: KeyboardEvent) { open.value = false; (event.currentTarget as HTMLElement).previousElementSibling?.dispatchEvent(new Event('focus')) }
</script>
