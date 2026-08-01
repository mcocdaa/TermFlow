<template><span class="status-pill" :data-online="online"><span class="status-dot" aria-hidden="true" />{{ label ?? (online ? '在线' : '离线') }}</span></template>
<script setup lang="ts">
defineProps<{ online: boolean; label?: string }>()
</script>
