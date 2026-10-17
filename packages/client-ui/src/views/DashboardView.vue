<template>
  <section class="dashboard-view" aria-labelledby="dashboard-title">
    <header class="page-heading"><div><p class="eyebrow">{{ t('dashboard.eyebrow') }}</p><h1 id="dashboard-title">{{ t('dashboard.title') }}</h1></div><div class="page-heading-actions"><button class="text-button" type="button" @click="refresh">{{ t('dashboard.refresh') }}</button></div></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading && !snapshot" class="muted" aria-live="polite">{{ t('dashboard.loading') }}</p>
    <template v-if="snapshot">
      <div class="metrics-grid" :aria-label="t('dashboard.metricsAria')">
        <MetricCard :label="t('dashboard.metric.onlineTerms')" :value="snapshot.metrics.online_terms" :detail="t('dashboard.metric.totalTerms', { count: snapshot.metrics.total_terms })" :help="t('dashboard.metric.onlineTermsHelp', { count: snapshot.metrics.total_terms })" />
        <MetricCard :label="t('dashboard.metric.activePanes')" :value="snapshot.metrics.active_panes" :help="t('dashboard.metric.activePanesHelp')" />
        <MetricCard :label="t('dashboard.metric.interactions24h')" :value="snapshot.metrics.interactions_24h" :help="t('dashboard.metric.interactions24hHelp')" />
        <MetricCard :label="t('dashboard.metric.computers')" :value="snapshot.metrics.computers" :help="t('dashboard.metric.computersHelp', { count: onlineComputerCount })" />
      </div>
      <div class="computer-grid"><ComputerCard v-for="computer in snapshot.computers" :key="computer.installation_id" :computer="computer" @request-delete="requestDelete" /></div>
    </template>
    <DeleteTermDialog v-if="selectedForDeletion" :term="selectedForDeletion" :pending="deletePending" :error="deleteError" :cleanup-job-id="cleanupJobId" @confirm="confirmDelete" @cancel="cancelDelete" />
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { computed, ref } from 'vue'
import ComputerCard from '../components/dashboard/ComputerCard.vue'
import DeleteTermDialog from '../components/dashboard/DeleteTermDialog.vue'
import MetricCard from '../components/dashboard/MetricCard.vue'
import { useDashboard } from '../composables/useDashboard'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'
import type { TermSummary } from '../types'
const { t } = useI18n()
const { snapshot, loading, message, refresh } = useDashboard()
const runtime = useClientRuntime()
const onlineComputerCount = computed(() => snapshot.value?.computers.filter((computer) => computer.online).length ?? 0)
const selectedForDeletion = ref<TermSummary | null>(null)
const deletePending = ref(false)
const deleteError = ref('')
const cleanupJobId = ref('')

function requestDelete(term: TermSummary) {
  cleanupJobId.value = ''
  selectedForDeletion.value = term
  deleteError.value = ''
}

function cancelDelete() {
  if (deletePending.value) return
  selectedForDeletion.value = null
  deleteError.value = ''
}

function deleteMessage(error: unknown) {
  if (error instanceof ApiError && error.code === 'instance_online') return t('dashboard.deleteError.online')
  if (error instanceof ApiError && error.code === 'instance_not_found') return t('dashboard.deleteError.notFound')
  return error instanceof ApiError ? error.message : t('dashboard.deleteError.generic')
}

async function confirmDelete(instanceId: string) {
  if (deletePending.value) return
  deletePending.value = true
  deleteError.value = ''
  try {
    const result = await runtime.api.terms.remove(instanceId)
    await refresh()
    if (result.state === 'deletion_pending') {
      cleanupJobId.value = result.cleanupJobId
      return
    }
    selectedForDeletion.value = null
  } catch (error) {
    deleteError.value = deleteMessage(error)
    if (error instanceof ApiError && error.code === 'instance_not_found') await refresh()
  } finally {
    deletePending.value = false
  }
}
</script>
