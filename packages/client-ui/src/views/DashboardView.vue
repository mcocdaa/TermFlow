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
      <div class="computer-grid"><ComputerCard v-for="computer in snapshot.computers" :key="computer.installation_id" :computer="computer" /></div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ComputerCard from '../components/dashboard/ComputerCard.vue'
import MetricCard from '../components/dashboard/MetricCard.vue'
import { useDashboard } from '../composables/useDashboard'
const { snapshot, loading, message, refresh } = useDashboard()
const onlineComputerCount = computed(() => snapshot.value?.computers.filter((computer) => computer.online).length ?? 0)
</script>
