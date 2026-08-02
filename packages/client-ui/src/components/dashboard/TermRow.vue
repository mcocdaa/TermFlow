<template>
  <component :is="term.online ? RouterLink : 'article'" class="term-row term-card" :class="{ 'term-row-link': term.online, 'term-row-offline': !term.online }" :data-term-id="term.instance_id" :to="term.online ? route : undefined" :aria-label="term.online ? `打开终端：${term.name}` : undefined" :aria-disabled="term.online ? undefined : 'true'">
    <div class="term-primary">
      <strong>{{ term.name }}</strong>
      <code>{{ term.current_command || '—' }}</code>
    </div>
    <div class="term-counts"><span>{{ term.window_count }} Windows</span><span>{{ term.pane_count }} Panes</span></div>
    <StatusPill :online="term.online" />
    <time v-if="term.last_seen_at" class="term-last-seen" :datetime="term.last_seen_at">{{ formatTime(term.last_seen_at) }}</time>
    <span v-else class="muted term-last-seen">尚未在线</span>
  </component>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import type { TermSummary } from '../../types'
import { formatBRecordedTime } from '../../utils/time'
import StatusPill from './StatusPill.vue'

const props = defineProps<{ term: TermSummary }>()
const route = `/terms/${encodeURIComponent(props.term.instance_id)}`
const formatTime = formatBRecordedTime
</script>
