<template>
  <div v-if="panes.length" class="titlebar-menu pane-focus-menu">
    <button class="titlebar-button" type="button" :aria-expanded="open" aria-haspopup="menu" @click="open = !open">聚焦 Pane <span aria-hidden="true">▾</span></button>
    <div v-if="open" class="floating-menu" role="menu" aria-label="聚焦 Pane">
      <button v-for="pane in panes" :key="pane.pane_id" type="button" role="menuitem" @click="select(pane)">{{ pane.title || pane.pane_id }} · {{ pane.current_command }}</button>
      <button type="button" role="menuitem" @click="reset">显示完整终端</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { PaneTopologyDto } from '../../api/types'
defineProps<{ panes: PaneTopologyDto[] }>()
const emit = defineEmits<{ focus: [pane: PaneTopologyDto]; reset: [] }>()
const open = ref(false)
function select(pane: PaneTopologyDto) { emit('focus', pane); open.value = false }
function reset() { emit('reset'); open.value = false }
</script>
