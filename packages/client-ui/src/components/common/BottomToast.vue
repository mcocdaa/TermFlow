<template>
  <p
    v-if="active"
    data-bottom-toast
    class="bottom-toast"
    :data-tone="active.tone"
    :role="active.tone === 'error' ? 'alert' : 'status'"
  >{{ active.text }}</p>
</template>

<script setup lang="ts">
import { computed, inject } from 'vue'
import type { BottomToastMessage, BottomToastTone } from '../../composables/useBottomToast'
import { bottomToastKey } from '../../runtimeKey'

const props = defineProps<{
  message?: string
  tone?: BottomToastTone
}>()

const controller = inject(bottomToastKey, undefined)
const active = computed<BottomToastMessage | null>(() => {
  if (props.message !== undefined) return { text: props.message, tone: props.tone ?? 'success' }
  return controller?.current.value ?? null
})
</script>
