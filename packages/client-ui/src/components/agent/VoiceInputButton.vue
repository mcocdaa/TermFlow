<template>
  <div v-if="rendered()" class="voice-input">
    <div v-if="state.status === 'uploading'" class="voice-uploading">
      <span class="voice-spinner" aria-hidden="true" />
      <span>正在转写…</span>
      <span class="voice-seconds" data-voice-upload-seconds>{{ uploadElapsedSeconds }}s</span>
      <button type="button" class="text-button" data-action="cancel-upload" @click="bridge?.cancelUpload()">取消</button>
    </div>
    <div v-else-if="state.status === 'failure'" class="voice-failure">
      <button v-if="state.failure.retryable" type="button" class="compact-secondary-button" data-action="retry-upload" @click="bridge?.retryUpload()">重试</button>
      <button type="button" class="compact-secondary-button" data-action="re-record" @click="bridge?.reRecord()">重新录音</button>
    </div>
    <template v-else-if="state.status !== 'disabled' && state.status !== 'unavailable'">
      <button
        type="button"
        class="voice-input-button"
        :class="{ recording, 'cancel-pending': cancelPending }"
        :aria-pressed="recording"
        aria-label="按住说话"
        :aria-describedby="hintId"
        @pointerdown="onPointerDown"
        @keydown="onKeydown"
        @keyup="onKeyup"
      >
        <span class="voice-hold-icon" aria-hidden="true"><Mic :size="20" /></span>
        <span v-if="recording" class="voice-recording-indicator">
          <span class="voice-recording-dot" aria-hidden="true" />
          <span class="voice-seconds" data-voice-seconds>{{ elapsedSeconds }}s</span>
        </span>
        <span v-else class="voice-hold-label">按住说话</span>
      </button>
      <p :id="hintId" class="voice-hint">{{ hint }}</p>
    </template>
    <p class="sr-only" aria-live="polite" data-voice-announcement>{{ announcement }}</p>
  </div>
</template>

<script setup lang="ts">
import { Mic } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useClientRuntime } from '../../runtime'
import { useInjectedVoiceDraft } from '../../composables/useVoiceDraft'

/**
 * Press/hold voice button (M7b spec §4.11/§4.4).
 *
 * Rendered only when all three conditions hold: `runtime.voice` exists ∧
 * `voice.enabled()` ∧ `speechToTextEnabled` — text-only Agent chat is
 * unaffected otherwise (§4.2). `bindingId`/`conversationId` mirror the
 * `useVoiceDraft` options of the owning composer (M6b passes both; the
 * controller — not this component — owns the actual wiring).
 */
const props = defineProps<{
  bindingId: string
  conversationId: string
  speechToTextEnabled: boolean
}>()
const emit = defineEmits<{ 'draft-submitted': [draftId: string] }>()

const runtime = useClientRuntime()
const bridge = useInjectedVoiceDraft()
const hintId = useId()

function rendered(): boolean {
  // Re-evaluated on every render (not a cached computed) so the dynamic
  // `voice.enabled()` gate is re-checked whenever the bridge state changes.
  if (bridge === undefined) return false
  const voice = runtime.voice
  return voice !== undefined && voice.enabled() && props.speechToTextEnabled
}

const state = computed(() => (bridge !== undefined ? bridge.state.value : { status: 'idle' as const }))
const recording = computed(() => state.value.status === 'recording')
const cancelPending = computed(() => state.value.status === 'recording' && state.value.cancelPending)
const hint = computed(() => {
  if (cancelPending.value) return '松手取消'
  if (recording.value) return '上滑取消'
  return '按住说话，松开发送，上滑取消'
})
const announcement = computed(() => {
  switch (state.value.status) {
    case 'recording':
      return '录音中'
    case 'uploading':
      return '正在转写'
    case 'draft':
      return '转写完成，请确认'
    default:
      return ''
  }
})

// --- stopwatch (presentational; the controller owns the true duration) ---
const elapsedSeconds = ref(0)
let interval: unknown | null = null
watch(recording, (active) => {
  if (active) {
    elapsedSeconds.value = 0
    interval = runtime.clock.setInterval(() => {
      elapsedSeconds.value += 1
    }, 1000)
  } else {
    if (interval !== null) runtime.clock.clearInterval(interval)
    interval = null
  }
})

// --- upload elapsed seconds (M7b spec §4.6: no progress, show elapsed) ---
const uploadElapsedSeconds = ref(0)
let uploadInterval: unknown | null = null
watch(
  () => state.value.status === 'uploading',
  (active) => {
    if (active) {
      uploadElapsedSeconds.value = 0
      uploadInterval = runtime.clock.setInterval(() => {
        uploadElapsedSeconds.value += 1
      }, 1000)
    } else {
      if (uploadInterval !== null) runtime.clock.clearInterval(uploadInterval)
      uploadInterval = null
    }
  },
)
onBeforeUnmount(() => {
  if (interval !== null) runtime.clock.clearInterval(interval)
  if (uploadInterval !== null) runtime.clock.clearInterval(uploadInterval)
})

// --- press/hold gesture (pointer events cover touch/mouse/pen) ---
// Pointer move/up are tracked on the window so a slide-up past the button
// bounds keeps delivering events (pointer capture is unavailable in some
// WebViews and jsdom).
let activePointerId: number | null = null
let originY = 0

function finishPointer() {
  activePointerId = null
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerCancel)
}

function onPointerDown(event: PointerEvent) {
  if (bridge === undefined || activePointerId !== null) return
  activePointerId = event.pointerId
  originY = event.clientY
  event.preventDefault()
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('pointercancel', onPointerCancel)
  bridge.press()
}

function onPointerMove(event: PointerEvent) {
  if (bridge === undefined || event.pointerId !== activePointerId) return
  bridge.slide(Math.max(0, originY - event.clientY))
}

function onPointerUp(event: PointerEvent) {
  if (bridge === undefined || event.pointerId !== activePointerId) return
  finishPointer()
  bridge.release()
}

function onPointerCancel(event: PointerEvent) {
  if (bridge === undefined || event.pointerId !== activePointerId) return
  finishPointer()
  bridge.cancel()
}

onBeforeUnmount(finishPointer)

// --- keyboard equivalent (Space/Enter; preventDefault stops the native
//     keyup→click activation that would double-trigger) ---
function onKeydown(event: KeyboardEvent) {
  if (bridge === undefined) return
  if (event.key !== ' ' && event.key !== 'Enter') return
  event.preventDefault()
  if (event.repeat) return
  bridge.press()
}

function onKeyup(event: KeyboardEvent) {
  if (bridge === undefined) return
  if (event.key !== ' ' && event.key !== 'Enter') return
  bridge.release()
}

// --- forward submit success as `draft-submitted` (M6b embed contract) ---
let unsubscribeSubmitted: (() => void) | undefined
onMounted(() => {
  unsubscribeSubmitted = bridge?.onDraftSubmitted((draftId) => emit('draft-submitted', draftId))
})
onBeforeUnmount(() => unsubscribeSubmitted?.())
</script>
