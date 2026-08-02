<template>
  <section class="dashboard-view" aria-labelledby="dashboard-title">
    <header class="page-heading"><div><p class="eyebrow">实时状态</p><h1 id="dashboard-title">控制中心</h1></div><button class="text-button" type="button" @click="refresh">刷新</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading && !snapshot" class="muted" aria-live="polite">正在读取状态…</p>
    <template v-if="snapshot">
      <div class="metrics-grid" aria-label="关键指标">
        <MetricCard label="在线 Terms" :value="snapshot.metrics.online_terms" :detail="`共 ${snapshot.metrics.total_terms} Terms`" :help="`当前在线并可远程控制的 Term，共 ${snapshot.metrics.total_terms} 个 Term。`" />
        <MetricCard label="活动 Panes" :value="snapshot.metrics.active_panes" help="在线 Term 中当前处于活动状态的 Pane 数量。" />
        <MetricCard label="24 小时交互" :value="snapshot.metrics.interactions_24h" help="过去 24 小时内由控制节点记录的交互次数。" />
        <MetricCard label="Computers" :value="snapshot.metrics.computers" :help="`已注册到当前控制节点的 Computer 数量，其中 ${onlineComputerCount} 台在线。`" />
      </div>
      <div class="computer-grid"><ComputerCard v-for="computer in snapshot.computers" :key="computer.installation_id" :computer="computer" @request-delete="requestDelete" /></div>
    </template>
    <DeleteTermDialog v-if="selectedForDeletion" :term="selectedForDeletion" :pending="deletePending" :error="deleteError" @confirm="confirmDelete" @cancel="cancelDelete" />
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { computed, ref } from 'vue'
import ComputerCard from '../components/dashboard/ComputerCard.vue'
import DeleteTermDialog from '../components/dashboard/DeleteTermDialog.vue'
import MetricCard from '../components/dashboard/MetricCard.vue'
import { useDashboard } from '../composables/useDashboard'
import { useClientRuntime } from '../runtime'
import type { TermSummary } from '../types'
const { snapshot, loading, message, refresh } = useDashboard()
const runtime = useClientRuntime()
const onlineComputerCount = computed(() => snapshot.value?.computers.filter((computer) => computer.online).length ?? 0)
const selectedForDeletion = ref<TermSummary | null>(null)
const deletePending = ref(false)
const deleteError = ref('')

function requestDelete(term: TermSummary) {
  selectedForDeletion.value = term
  deleteError.value = ''
}

function cancelDelete() {
  if (deletePending.value) return
  selectedForDeletion.value = null
  deleteError.value = ''
}

function deleteMessage(error: unknown) {
  if (error instanceof ApiError && error.code === 'instance_online') return 'Term 已重新上线，无法删除。'
  if (error instanceof ApiError && error.code === 'instance_not_found') return 'Term 已不存在；列表已按服务器状态刷新。'
  return error instanceof ApiError ? error.message : '无法删除 Term，请重试。'
}

async function confirmDelete(instanceId: string) {
  if (deletePending.value) return
  deletePending.value = true
  deleteError.value = ''
  try {
    await runtime.api.terms.remove(instanceId)
    await refresh()
    selectedForDeletion.value = null
  } catch (error) {
    deleteError.value = deleteMessage(error)
    if (error instanceof ApiError && error.code === 'instance_not_found') await refresh()
  } finally {
    deletePending.value = false
  }
}
</script>
