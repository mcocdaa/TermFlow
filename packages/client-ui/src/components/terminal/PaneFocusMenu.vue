<template>
  <div v-if="panes.length" class="titlebar-menu pane-focus-menu">
    <button ref="trigger" data-action="toggle-pane-focus-menu" class="titlebar-button" :class="{ 'is-open': open }" type="button" aria-label="聚焦 Pane" :aria-expanded="open" aria-haspopup="menu" @click="$emit('update:open', !open)"><Focus :size="16" aria-hidden="true" /><span class="control-label">聚焦 Pane</span><ChevronDown class="menu-chevron" :size="15" aria-hidden="true" /></button>
    <div v-if="open" class="floating-menu" role="menu" aria-label="聚焦 Pane" @keydown.esc.prevent="closeAndFocus">
      <button v-for="pane in panes" :key="pane.pane_id" type="button" role="menuitem" @click="select(pane)">{{ pane.title || pane.pane_id }} · {{ pane.current_command }}</button>
      <button type="button" role="menuitem" @click="reset">显示完整终端</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, Focus } from '@lucide/vue'
import { nextTick, ref } from 'vue'
import type { PaneTopology } from '../../types'
withDefaults(defineProps<{ panes: PaneTopology[]; open?: boolean }>(), { open: false })
const emit = defineEmits<{ focus: [pane: PaneTopology]; reset: []; 'update:open': [open: boolean] }>()
const trigger = ref<HTMLButtonElement | null>(null)
function select(pane: PaneTopology) { emit('focus', pane); emit('update:open', false) }
function reset() { emit('reset'); emit('update:open', false) }
async function closeAndFocus() { emit('update:open', false); await nextTick(); trigger.value?.focus() }
</script>
