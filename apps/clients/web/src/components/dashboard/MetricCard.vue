<template><article class="metric-card"><span>{{ label }}</span><strong>{{ value }}</strong><small v-if="detail">{{ detail }}</small></article></template>
<script setup lang="ts">
defineProps<{ label: string; value: number; detail?: string }>()
</script>
