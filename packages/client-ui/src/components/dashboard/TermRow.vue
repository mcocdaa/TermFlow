<template>
  <component :is="term.online ? RouterLink : 'article'" class="term-row term-card" :class="{ 'term-row-link': term.online, 'term-row-offline': !term.online }" :data-term-id="term.instance_id" :to="term.online ? route : undefined" :aria-label="term.online ? t('dashboard.openTermAria', { name: term.name }) : undefined">
    <div class="term-primary">
      <strong>{{ term.name }}</strong>
      <code>{{ term.current_command || '—' }}</code>
    </div>
    <div class="term-counts"><span>{{ term.window_count }} {{ t('dashboard.windows') }}</span><span>{{ term.pane_count }} {{ t('dashboard.panes') }}</span></div>
    <StatusPill :online="term.online" />
    <time v-if="term.last_seen_at" class="term-last-seen" :datetime="term.last_seen_at">{{ formatTime(term.last_seen_at) }}</time>
    <span v-else class="muted term-last-seen">{{ t('dashboard.neverOnline') }}</span>
    <button v-if="!term.online" data-action="delete-offline-term" class="icon-button icon-only destructive term-delete-button" type="button" :aria-label="t('dashboard.deleteOfflineTermAria', { name: term.name })" :title="t('dashboard.deleteOfflineTermAria', { name: term.name })" @click="$emit('request-delete', term)"><Trash2 :size="18" aria-hidden="true" /></button>
  </component>
</template>

<script setup lang="ts">
import { Trash2 } from '@lucide/vue'
import { RouterLink } from 'vue-router'
import { useI18n } from '../../i18n'
import type { TermSummary } from '../../types'
import { formatBRecordedTime } from '../../utils/time'
import StatusPill from './StatusPill.vue'

const { t } = useI18n()
const props = defineProps<{ term: TermSummary }>()
defineEmits<{ 'request-delete': [term: TermSummary] }>()
const route = `/terms/${encodeURIComponent(props.term.instance_id)}`
const formatTime = formatBRecordedTime
</script>
