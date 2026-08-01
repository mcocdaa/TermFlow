<template>
  <section class="computers-view" aria-labelledby="computers-title">
    <header class="page-heading"><div><p class="eyebrow">设备与注册</p><h1 id="computers-title">电脑管理</h1></div><button class="primary-button" type="button" @click="showEnrollment = true">添加电脑</button></header>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
    <p v-if="loading" class="muted">正在读取 Computers…</p>
    <ComputerTable v-else :computers="computers" />
    <EnrollmentDialog v-if="showEnrollment" @closed="showEnrollment = false" />
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { listComputers } from '../api/computers'
import { ApiError } from '../api/http'
import type { ComputerSummaryDto } from '../api/types'
import ComputerTable from '../components/computers/ComputerTable.vue'
import EnrollmentDialog from '../components/computers/EnrollmentDialog.vue'

const computers = ref<ComputerSummaryDto[]>([])
const loading = ref(true)
const message = ref('')
const showEnrollment = ref(false)
const controller = new AbortController()
onMounted(async () => {
  try { computers.value = (await listComputers(controller.signal)).computers }
  catch (error) { if (!(error instanceof ApiError) || error.kind !== 'aborted') message.value = error instanceof ApiError ? error.message : '无法加载 Computers。' }
  finally { loading.value = false }
})
onBeforeUnmount(() => controller.abort())
</script>
