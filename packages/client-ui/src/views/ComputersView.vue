<template>
  <section class="computers-view" aria-labelledby="computers-title">
    <header class="page-heading"><div><p class="eyebrow">{{ t('computers.eyebrow') }}</p><h1 id="computers-title">{{ t('computers.title') }}</h1></div><button class="primary-button" type="button" @click="showEnrollment = true">{{ t('computers.add') }}</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading" class="muted">{{ t('computers.loading') }}</p>
    <ComputerTable v-else :computers="computers" @remove="requestRemove" />
    <EnrollmentDialog v-if="showEnrollment" @added="onComputerAdded" @closed="showEnrollment = false" />
    <DeleteComputerDialog
      v-if="selectedForDeletion"
      :computer="selectedForDeletion"
      :pending="deletingId !== null"
      :error="deleteError"
      :cleanup-job-id="cleanupJobId"
      @confirm="confirmRemove"
      @cancel="cancelRemove"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, safeMessageFor, type ApiErrorKind } from '@termflow/client-core'
import ComputerTable from '../components/computers/ComputerTable.vue'
import EnrollmentDialog from '../components/computers/EnrollmentDialog.vue'
import DeleteComputerDialog from '../components/computers/DeleteComputerDialog.vue'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from '../composables/useBottomToast'
import type { ComputerSummary } from '../types'

const { t, locale } = useI18n()
const runtime = useClientRuntime()
const toast = useBottomToast()
const computers = ref<ComputerSummary[]>([])
const loading = ref(true)
const errorState = ref<{ type: 'api'; kind: ApiErrorKind } | { type: 'key'; key: string } | null>(null)
const message = computed(() => {
  void locale.value
  if (errorState.value === null) return ''
  return errorState.value.type === 'api' ? safeMessageFor(errorState.value.kind) : t(errorState.value.key)
})
const showEnrollment = ref(false)
const deletingId = ref<string | null>(null)
const selectedForDeletion = ref<ComputerSummary | null>(null)
const deleteErrorState = ref<{ type: 'api'; kind: ApiErrorKind } | { type: 'key'; key: string } | null>(null)
const deleteError = computed(() => {
  void locale.value
  if (deleteErrorState.value === null) return ''
  return deleteErrorState.value.type === 'api' ? safeMessageFor(deleteErrorState.value.kind) : t(deleteErrorState.value.key)
})
const cleanupJobId = ref('')
const controller = new AbortController()
async function loadComputers() {
  loading.value = true
  try { computers.value = (await runtime.api.computers.list(controller.signal)).computers }
  catch (error) {
    if (!(error instanceof ApiError) || error.kind !== 'aborted') {
      errorState.value = error instanceof ApiError ? { type: 'api', kind: error.kind } : { type: 'key', key: 'computers.loadError' }
    }
  }
  finally { loading.value = false }
}
function requestRemove(computer: ComputerSummary) {
  cleanupJobId.value = ''
  if (deletingId.value !== null) return
  selectedForDeletion.value = computer
  deleteErrorState.value = null
}
function cancelRemove() {
  if (deletingId.value !== null) return
  selectedForDeletion.value = null
  deleteErrorState.value = null
}
async function confirmRemove(installationId: string) {
  if (deletingId.value !== null) return
  deletingId.value = installationId
  deleteErrorState.value = null
  try {
    const result = await runtime.api.computers.remove(installationId)
    computers.value = computers.value.filter((candidate) => candidate.installation_id !== installationId)
    if (result.state === 'deletion_pending') {
      cleanupJobId.value = result.cleanupJobId
      return
    }
    selectedForDeletion.value = null
    toast.show({ text: t('computers.deletedToast'), tone: 'success' })
  } catch (error) {
    deleteErrorState.value = error instanceof ApiError ? { type: 'api', kind: error.kind } : { type: 'key', key: 'computers.deleteError' }
  }
  finally { deletingId.value = null }
}
async function onComputerAdded() {
  showEnrollment.value = false
  errorState.value = null
  await loadComputers()
  if (!message.value) toast.show({ text: t('computers.addedToast'), tone: 'success' })
}
onMounted(() => { void loadComputers() })
onBeforeUnmount(() => {
  controller.abort()
})
</script>
