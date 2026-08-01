<template>
  <div class="titlebar-menu">
    <button ref="trigger" data-action="toggle-display-menu" class="titlebar-button" :class="{ 'is-open': open }" type="button" aria-label="显示设置" aria-haspopup="menu" :aria-expanded="open" @click="toggleMenu"><MonitorCog :size="16" aria-hidden="true" /><span class="control-label">显示</span><ChevronDown class="menu-chevron" :size="15" aria-hidden="true" /></button>
    <div v-if="open" class="floating-menu display-menu" role="menu" aria-label="终端显示比例" @keydown.esc.prevent="closeAndFocus" @keydown.down.prevent="moveFocus(1)" @keydown.up.prevent="moveFocus(-1)">
      <button v-for="choice in choices" ref="choiceButtons" :key="choice.id" type="button" role="menuitemradio" :aria-label="choice.label" :aria-checked="modelValue === choice.id" @click="select(choice.id)">
        <CircleDot v-if="modelValue === choice.id" :size="16" aria-hidden="true" /><Circle v-else :size="16" aria-hidden="true" /><span>{{ choice.label }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, Circle, CircleDot, MonitorCog } from '@lucide/vue'
import { nextTick, ref, watch } from 'vue'
import type { DisplayMode } from '../../terminal/viewport'
const props = withDefaults(defineProps<{ modelValue: DisplayMode; open?: boolean }>(), { open: false })
const emit = defineEmits<{ 'update:modelValue': [mode: DisplayMode]; 'update:open': [open: boolean] }>()
const trigger = ref<HTMLButtonElement | null>(null)
const choiceButtons = ref<HTMLButtonElement[]>([])
const choices: Array<{ id: DisplayMode; label: string }> = [
  { id: 'scale-50', label: '50%' }, { id: 'scale-75', label: '75%' }, { id: 'font-100', label: '100% 实际字号' }, { id: 'fit', label: '适应窗口' },
]
function toggleMenu() { emit('update:open', !props.open) }
watch(() => props.open, async (open) => {
  if (!open) return
  await nextTick()
  choiceButtons.value[choices.findIndex((choice) => choice.id === props.modelValue)]?.focus()
})
async function select(mode: DisplayMode) { emit('update:modelValue', mode); emit('update:open', false); await nextTick(); trigger.value?.focus() }
function moveFocus(offset: number) {
  const current = choiceButtons.value.findIndex((button) => button === document.activeElement)
  choiceButtons.value[(current + offset + choiceButtons.value.length) % choiceButtons.value.length]?.focus()
}
async function closeAndFocus() { emit('update:open', false); await nextTick(); trigger.value?.focus() }
</script>
