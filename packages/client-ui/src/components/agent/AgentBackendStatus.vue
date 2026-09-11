<template>
  <span v-if="displayState !== 'unknown'" class="agent-backend-status" :class="`agent-backend-status--${displayState}`" :data-agent-backend-state="dataState">
    {{ label }}
  </span>
</template>

<script setup lang="ts">
//: Backend runtime status badge (M6b spec §4.7). The raw STATE_DELTA
//: value is mapped to a Chinese label. Before the first delta, null means the
//: stream is still opening, so the user sees the actionable "连接中" state;
//: unknown wire values remain "未知" while the data attribute keeps the raw
//: value verbatim so wire drift never hides data.
import { computed } from 'vue'
import { isBackendRuntimeState, type BackendRuntimeState } from '@termflow/client-core'

const props = defineProps<{
  /** Raw STATE_DELTA ``/backend`` value from the history reducer (null before the first delta). */
  state: string | null
}>()

const LABELS: Record<BackendRuntimeState, string> = {
  connecting: '连接中',
  idle: '空闲',
  busy: '处理中',
  retry: '重试中',
  ready: '就绪',
  unavailable: '不可用',
  context_lost: '上下文丢失',
  reconciling: '恢复中',
  closed: '已关闭',
}

const known = computed<BackendRuntimeState | null>(() =>
  props.state !== null && isBackendRuntimeState(props.state) ? props.state : null,
)
const displayState = computed(() => props.state === null ? 'connecting' : (known.value !== null ? known.value : 'unknown'))
// Keep the diagnostic attribute faithful to the wire value: null is exposed
// as ``unknown`` while the visual label still tells the user it is opening.
const dataState = computed(() => props.state ?? 'unknown')
const label = computed(() => props.state === null ? LABELS.connecting : (known.value !== null ? LABELS[known.value] : '未知'))
</script>
