<template>
  <section class="dashboard-view" aria-labelledby="dashboard-title">
    <header class="page-heading"><div><p class="eyebrow">实时状态</p><h1 id="dashboard-title">控制中心</h1></div><button class="text-button" type="button" @click="refresh">刷新</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading && !snapshot" class="muted" aria-live="polite">正在读取状态…</p>
    <template v-if="snapshot">
      <div class="metrics-grid" aria-label="关键指标">
        <MetricCard label="在线 Terms" :value="snapshot.metrics.online_terms" />
        <MetricCard label="活动 Panes" :value="snapshot.metrics.active_panes" />
        <MetricCard label="24 小时交互" :value="snapshot.metrics.interactions_24h" />
        <MetricCard label="Computers" :value="snapshot.metrics.computers" />
      </div>
      <div class="computer-grid"><ComputerCard v-for="computer in snapshot.computers" :key="computer.installation_id" :computer="computer" /></div>
    </template>
  </section>
</template>

<script setup lang="ts">
import ComputerCard from '../components/dashboard/ComputerCard.vue'
import MetricCard from '../components/dashboard/MetricCard.vue'
import { useDashboard } from '../composables/useDashboard'
const { snapshot, loading, message, refresh } = useDashboard()
</script>
