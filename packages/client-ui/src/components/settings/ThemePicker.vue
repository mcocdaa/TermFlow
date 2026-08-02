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
      ref="radioButtons"
      :key="theme.id"
      class="theme-option"
      type="button"
      role="radio"
      :aria-label="theme.label"
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
import { nextTick, ref } from 'vue'
import type { ThemeId } from '@termflow/design-tokens'
import { useTheme } from '../../theme/theme'

const { active: activeTheme, select: selectTheme } = useTheme()

const themes: ReadonlyArray<{ id: ThemeId; label: string }> = [
  { id: 'graphite-signal', label: '石墨信号' },
  { id: 'cloud-cobalt', label: '云端钴蓝' },
  { id: 'midnight-indigo', label: '午夜靛蓝' },
]
const radioButtons = ref<HTMLButtonElement[]>([])

function choose(id: ThemeId) {
  selectTheme(id)
}

async function move(offset: number) {
  const current = themes.findIndex((theme) => theme.id === activeTheme.value)
  const next = (current + offset + themes.length) % themes.length
  const theme = themes[next]
  if (theme === undefined) return
  choose(theme.id)
  await nextTick()
  radioButtons.value[next]?.focus()
}
</script>
