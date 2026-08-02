<template>
  <article class="metric-card" :class="{ 'metric-card-has-help': help }" :tabindex="help ? 0 : undefined" :aria-describedby="help ? tooltipId : undefined">
    <span>{{ label }}</span>
    <strong>{{ value }}</strong>
    <small v-if="detail">{{ detail }}</small>
    <p v-if="help" :id="tooltipId" class="metric-help" role="tooltip">{{ help }}</p>
  </article>
</template>
<script setup lang="ts">
import { useId } from 'vue'

defineProps<{ label: string; value: number; detail?: string; help?: string }>()
const tooltipId = `metric-help-${useId()}`
</script>
