<template>
  <div
    class="theme-picker"
    role="radiogroup"
    aria-label="界面主题"
    @keydown.right.prevent="move(1)"
    @keydown.down.prevent="move(1)"
    @keydown.left.prevent="move(-1)"
    @keydown.up.prevent="move(-1)"
  >
    <button
      v-for="theme in themes"
      :key="theme.id"
      class="theme-option"
      type="button"
      role="radio"
      :aria-checked="activeTheme === theme.id"
      :tabindex="activeTheme === theme.id ? 0 : -1"
      @click="choose(theme.id)"
    >
      <span class="theme-swatch" :data-swatch="theme.id" aria-hidden="true" />
      <span>{{ theme.label }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import type { ThemeId } from '@termflow/design-tokens'
import { activeTheme, selectTheme } from '../../stores/theme'

const themes: ReadonlyArray<{ id: ThemeId; label: string }> = [
  { id: 'graphite-signal', label: '石墨信号' },
  { id: 'cloud-cobalt', label: '云端钴蓝' },
  { id: 'midnight-indigo', label: '午夜靛蓝' },
]

function choose(id: ThemeId) {
  selectTheme(id)
}

function move(offset: number) {
  const current = themes.findIndex((theme) => theme.id === activeTheme.value)
  choose(themes[(current + offset + themes.length) % themes.length].id)
}
</script>
