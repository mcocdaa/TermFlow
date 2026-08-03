<template>
  <span data-context-help class="help-tooltip">
    <button
      v-bind="$attrs"
      class="icon-button icon-only help-tooltip-trigger"
      type="button"
      :aria-label="label"
      :aria-describedby="tooltipId"
    >
      <CircleHelp :size="17" aria-hidden="true" />
    </button>
    <span :id="tooltipId" role="tooltip">{{ text }}</span>
  </span>
</template>

<script setup lang="ts">
import { CircleHelp } from '@lucide/vue'

defineOptions({ inheritAttrs: false })
defineProps<{ label: string; text: string }>()
const tooltipId = `context-help-${crypto.randomUUID()}`
</script>
