<template><article class="metric-card"><span>{{ label }}</span><strong>{{ value }}</strong></article></template>
<script setup lang="ts">
defineProps<{ label: string; value: number }>()
</script>
