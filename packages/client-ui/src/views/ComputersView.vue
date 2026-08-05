<template>
  <section class="computers-view" aria-labelledby="computers-title">
    <header class="page-heading"><div><p class="eyebrow">设备与注册</p><h1 id="computers-title">电脑管理</h1></div><button class="primary-button" type="button" @click="showEnrollment = true">添加电脑</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading" class="muted">正在读取 Computers…</p>
    <ComputerTable v-else :computers="computers" @remove="requestRemove" />
    <EnrollmentDialog v-if="showEnrollment" @added="onComputerAdded" @closed="showEnrollment = false" />
    <DeleteComputerDialog
      v-if="selectedForDeletion"
      :computer="selectedForDeletion"
      :pending="deletingId !== null"
      :error="deleteError"
      @confirm="confirmRemove"
      @cancel="cancelRemove"
    />
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import ComputerTable from '../components/computers/ComputerTable.vue'
import EnrollmentDialog from '../components/computers/EnrollmentDialog.vue'
import DeleteComputerDialog from '../components/computers/DeleteComputerDialog.vue'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from '../composables/useBottomToast'
import type { ComputerSummary } from '../types'

const runtime = useClientRuntime()
const toast = useBottomToast()
const computers = ref<ComputerSummary[]>([])
const loading = ref(true)
const message = ref('')
const showEnrollment = ref(false)
const deletingId = ref<string | null>(null)
const selectedForDeletion = ref<ComputerSummary | null>(null)
const deleteError = ref('')
const controller = new AbortController()
async function loadComputers() {
  loading.value = true
  try { computers.value = (await runtime.api.computers.list(controller.signal)).computers }
  catch (error) { if (!(error instanceof ApiError) || error.kind !== 'aborted') message.value = error instanceof ApiError ? error.message : '无法加载 Computers。' }
  finally { loading.value = false }
}
function requestRemove(computer: ComputerSummary) {
  if (deletingId.value !== null) return
  selectedForDeletion.value = computer
  deleteError.value = ''
}
function cancelRemove() {
  if (deletingId.value !== null) return
  selectedForDeletion.value = null
  deleteError.value = ''
}
async function confirmRemove(installationId: string) {
  if (deletingId.value !== null) return
  deletingId.value = installationId
  deleteError.value = ''
  try {
    await runtime.api.computers.remove(installationId)
    computers.value = computers.value.filter((candidate) => candidate.installation_id !== installationId)
    selectedForDeletion.value = null
    toast.show({ text: '已删除', tone: 'success' })
  } catch (error) {
    deleteError.value = error instanceof ApiError ? error.message : '无法删除电脑。'
  }
  finally { deletingId.value = null }
}
async function onComputerAdded() {
  showEnrollment.value = false
  message.value = ''
  await loadComputers()
  if (!message.value) toast.show({ text: '已添加', tone: 'success' })
}
onMounted(() => { void loadComputers() })
onBeforeUnmount(() => {
  controller.abort()
})
</script>
