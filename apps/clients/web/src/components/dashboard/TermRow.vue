<template>
  <article class="term-row" :data-term-id="term.term_id">
    <div class="term-primary">
      <strong>{{ term.name }}</strong>
      <code>{{ term.pane_current_command || '—' }}</code>
    </div>
    <div class="term-counts"><span>{{ term.window_count }} Windows</span><span>{{ term.pane_count }} Panes</span></div>
    <StatusPill :online="term.online" />
    <time v-if="term.last_seen_at" :datetime="term.last_seen_at">{{ formatTime(term.last_seen_at) }}</time>
    <span v-else class="muted">尚未在线</span>
    <RouterLink v-if="term.online" class="term-open" :to="`/terms/${encodeURIComponent(term.term_id)}`">打开终端<span class="sr-only">：{{ term.name }}</span></RouterLink>
    <button v-else class="term-open" type="button" disabled title="Term 离线，无法打开终端">无法打开</button>
  </article>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import type { TermSummaryDto } from '../../api/types'
import StatusPill from './StatusPill.vue'

defineProps<{ term: TermSummaryDto }>()
const formatter = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
const formatTime = (value: string) => formatter.format(new Date(value))
</script>
