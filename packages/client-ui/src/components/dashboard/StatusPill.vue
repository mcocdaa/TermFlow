<template><span class="status-pill" :data-online="online"><span class="status-dot" aria-hidden="true" />{{ label ?? (online ? t('status.online') : t('status.offline')) }}</span></template>
<script setup lang="ts">
import { useI18n } from '../../i18n'
const { t } = useI18n()
defineProps<{ online: boolean; label?: string }>()
</script>
