<template>
  <header class="terminal-titlebar">
    <div class="terminal-identity"><span class="terminal-light" aria-hidden="true" /><span>{{ title }}</span></div>
    <div class="terminal-titlebar-actions"><DisplayMenu :model-value="displayMode" @update:model-value="$emit('update:displayMode', $event)" /><slot /></div>
  </header>
</template>

<script setup lang="ts">
import type { DisplayMode } from '../../terminal/viewport'
import DisplayMenu from './DisplayMenu.vue'
defineProps<{ title: string; displayMode: DisplayMode }>()
defineEmits<{ 'update:displayMode': [mode: DisplayMode] }>()
</script>
