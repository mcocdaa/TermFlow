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
import { useI18n } from '../../i18n'

const { t } = useI18n()
const props = defineProps<{
  /** Raw STATE_DELTA ``/backend`` value from the history reducer (null before the first delta). */
  state: string | null
}>()

const LABEL_KEYS: Record<BackendRuntimeState, string> = {
  connecting: 'agent.backend.connecting',
  idle: 'agent.backend.idle',
  busy: 'agent.backend.busy',
  retry: 'agent.backend.retry',
  ready: 'agent.backend.ready',
  unavailable: 'agent.backend.unavailable',
  context_lost: 'agent.backend.context_lost',
  reconciling: 'agent.backend.reconciling',
  closed: 'agent.backend.closed',
}

const known = computed<BackendRuntimeState | null>(() =>
  props.state !== null && isBackendRuntimeState(props.state) ? props.state : null,
)
const displayState = computed(() => props.state === null ? 'connecting' : (known.value !== null ? known.value : 'unknown'))
// Keep the diagnostic attribute faithful to the wire value: null is exposed
// as ``unknown`` while the visual label still tells the user it is opening.
const dataState = computed(() => props.state ?? 'unknown')
const label = computed(() => props.state === null ? t(LABEL_KEYS.connecting) : (known.value !== null ? t(LABEL_KEYS[known.value]) : t('common.unknown')))
</script>
