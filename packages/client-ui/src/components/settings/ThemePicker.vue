<template>
  <div
    class="theme-picker"
    data-layout="theme-grid"
    role="radiogroup"
    :aria-label="t('settings.appearance.heading')"
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
      :title="theme.label"
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
import { computed, nextTick, ref } from 'vue'
import type { ThemeId } from '@termflow/design-tokens'
import { useI18n } from '../../i18n'
import { useTheme } from '../../theme/theme'

const { t } = useI18n()
const { active: activeTheme, select: selectTheme } = useTheme()

const themes = computed<ReadonlyArray<{ id: ThemeId; label: string }>>(() => [
  { id: 'graphite-signal', label: t('settings.theme.graphite-signal') },
  { id: 'cloud-cobalt', label: t('settings.theme.cloud-cobalt') },
  { id: 'midnight-indigo', label: t('settings.theme.midnight-indigo') },
  { id: 'catppuccin-mocha', label: 'Catppuccin Mocha' },
  { id: 'catppuccin-latte', label: 'Catppuccin Latte' },
  { id: 'tokyo-night', label: 'Tokyo Night' },
  { id: 'nord', label: 'Nord' },
  { id: 'dracula', label: 'Dracula' },
  { id: 'solarized-dark', label: 'Solarized Dark' },
  { id: 'solarized-light', label: 'Solarized Light' },
])
const radioButtons = ref<HTMLButtonElement[]>([])

function choose(id: ThemeId) {
  selectTheme(id)
}

async function move(offset: number) {
  const current = themes.value.findIndex((theme) => theme.id === activeTheme.value)
  const next = (current + offset + themes.value.length) % themes.value.length
  const theme = themes.value[next]
  if (theme === undefined) return
  choose(theme.id)
  await nextTick()
  radioButtons.value[next]?.focus()
}
</script>
