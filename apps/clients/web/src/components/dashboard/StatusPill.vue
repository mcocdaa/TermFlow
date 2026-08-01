<template><span class="status-pill" :data-online="online"><span aria-hidden="true">●</span>{{ online ? '在线' : '离线' }}</span></template>
<script setup lang="ts">
defineProps<{ online: boolean }>()
</script>
