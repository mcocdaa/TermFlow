<template>
  <article class="computer-card">
    <header>
      <div><p class="eyebrow">Computer</p><h2>{{ computer.display_name }}</h2><p class="muted">{{ computer.hostname }} · {{ computer.platform }}</p></div>
      <StatusPill :online="computer.online" />
    </header>
    <div v-if="computer.terms.length" class="term-list"><TermRow v-for="term in computer.terms" :key="term.instance_id" :term="term" /></div>
    <p v-else class="empty-state">这台 Computer 还没有 Term。</p>
  </article>
</template>

<script setup lang="ts">
import type { ComputerSummaryDto } from '../../api/types'
import StatusPill from './StatusPill.vue'
import TermRow from './TermRow.vue'
defineProps<{ computer: ComputerSummaryDto }>()
</script>
