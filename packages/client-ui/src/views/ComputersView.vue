<template>
  <section class="computers-view" aria-labelledby="computers-title">
    <header class="page-heading"><div><p class="eyebrow">设备与注册</p><h1 id="computers-title">电脑管理</h1></div><button class="primary-button" type="button" @click="showEnrollment = true">添加电脑</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading" class="muted">正在读取 Computers…</p>
    <ComputerTable v-else :computers="computers" @remove="removeComputer" />
    <EnrollmentDialog v-if="showEnrollment" @added="onComputerAdded" @closed="showEnrollment = false" />
    <p v-if="deleteNotice" data-delete-notice class="computer-delete-toast" :data-tone="deleteNotice.tone" :role="deleteNotice.tone === 'error' ? 'alert' : 'status'">{{ deleteNotice.text }}</p>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import ComputerTable from '../components/computers/ComputerTable.vue'
import EnrollmentDialog from '../components/computers/EnrollmentDialog.vue'
import { useClientRuntime } from '../runtime'
import type { ComputerSummary } from '../types'

const runtime = useClientRuntime()
const computers = ref<ComputerSummary[]>([])
const loading = ref(true)
const message = ref('')
const showEnrollment = ref(false)
const deletingId = ref<string | null>(null)
type DeleteNotice = { text: string; tone: 'success' | 'error' }
const deleteNotice = ref<DeleteNotice | null>(null)
let deleteNoticeTimer: unknown | null = null
const controller = new AbortController()
function clearDeleteNoticeTimer() {
  if (deleteNoticeTimer !== null) runtime.clock.clearTimeout(deleteNoticeTimer)
  deleteNoticeTimer = null
}
function showDeleteNotice(notice: DeleteNotice) {
  clearDeleteNoticeTimer()
  deleteNotice.value = notice
  deleteNoticeTimer = runtime.clock.setTimeout(() => {
    deleteNotice.value = null
    deleteNoticeTimer = null
  }, 3_000)
}
async function loadComputers() {
  loading.value = true
  try { computers.value = (await runtime.api.computers.list(controller.signal)).computers }
  catch (error) { if (!(error instanceof ApiError) || error.kind !== 'aborted') message.value = error instanceof ApiError ? error.message : '无法加载 Computers。' }
  finally { loading.value = false }
}
async function removeComputer(computer: ComputerSummary) {
  if (deletingId.value !== null) return
  deletingId.value = computer.installation_id
  try {
    await runtime.api.computers.remove(computer.installation_id)
    computers.value = computers.value.filter((candidate) => candidate.installation_id !== computer.installation_id)
    showDeleteNotice({ text: '已删除', tone: 'success' })
  } catch (error) { showDeleteNotice({ text: error instanceof ApiError ? error.message : '无法删除电脑。', tone: 'error' }) }
  finally { deletingId.value = null }
}
async function onComputerAdded() {
  showEnrollment.value = false
  message.value = ''
  await loadComputers()
  if (!message.value) showDeleteNotice({ text: '已添加', tone: 'success' })
}
onMounted(() => { void loadComputers() })
onBeforeUnmount(() => {
  controller.abort()
  clearDeleteNoticeTimer()
})
</script>
