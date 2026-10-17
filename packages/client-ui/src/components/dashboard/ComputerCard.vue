<template>
  <article class="computer-card" :data-computer-id="computer.installation_id">
    <header>
      <div><p class="eyebrow">Computer</p><h2>{{ computer.display_name }}</h2><p v-if="metadata" class="muted">{{ metadata }}</p></div>
      <StatusPill :online="computer.online" />
    </header>
    <div v-if="computer.terms.length" class="term-list"><TermRow v-for="term in computer.terms" :key="term.instance_id" :term="term" @request-delete="$emit('request-delete', $event)" /></div>
    <p v-else class="empty-state">{{ t('dashboard.emptyTerms') }}</p>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '../../i18n'
import type { ComputerSummary } from '../../types'
import StatusPill from './StatusPill.vue'
import TermRow from './TermRow.vue'
const { t } = useI18n()
const props = defineProps<{ computer: ComputerSummary }>()
defineEmits<{ 'request-delete': [term: ComputerSummary['terms'][number]] }>()
const metadata = computed(() => [props.computer.hostname, props.computer.platform].filter(Boolean).join(' · '))
</script>
