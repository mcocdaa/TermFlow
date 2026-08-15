<template>
  <span class="agent-backend-status" :class="`agent-backend-status--${displayState}`" :data-agent-backend-state="state ?? 'unknown'">
    {{ label }}
  </span>
</template>

<script setup lang="ts">
//: Backend runtime status badge (M6b spec §4.7). The raw STATE_DELTA
//: value is mapped to a Chinese label; unknown wire values label "未知"
//: (never rendered as markup) while the data attribute keeps the raw
//: value verbatim so wire drift never hides data.
import { computed } from 'vue'
import { isBackendRuntimeState, type BackendRuntimeState } from '@termflow/client-core'

const props = defineProps<{
  /** Raw STATE_DELTA ``/backend`` value from the history reducer (null before the first delta). */
  state: string | null
}>()

const LABELS: Record<BackendRuntimeState, string> = {
  connecting: '连接中',
  ready: '就绪',
  unavailable: '不可用',
  context_lost: '上下文丢失',
  reconciling: '恢复中',
  closed: '已关闭',
}

const known = computed<BackendRuntimeState | null>(() =>
  props.state !== null && isBackendRuntimeState(props.state) ? props.state : null,
)
const displayState = computed(() => (known.value !== null ? known.value : 'unknown'))
const label = computed(() => (known.value !== null ? LABELS[known.value] : '未知'))
</script>
