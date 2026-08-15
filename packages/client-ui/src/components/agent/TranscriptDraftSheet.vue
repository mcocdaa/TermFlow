<template>
  <div v-if="draft" class="dialog-backdrop" @click.self="requestClose">
    <section
      ref="panel"
      class="dialog-panel voice-draft-sheet"
      role="dialog"
      aria-modal="true"
      aria-labelledby="voice-draft-title"
      aria-describedby="voice-draft-description"
      @keydown="onPanelKeydown"
    >
      <header>
        <h2 id="voice-draft-title">确认转写</h2>
      </header>
      <p id="voice-draft-description" class="sr-only">核对并编辑转写文本，然后确认发送或放弃。</p>
      <label for="voice-draft-text">转写文本（可编辑）</label>
      <textarea
        id="voice-draft-text"
        ref="textEl"
        v-model="text"
        :maxlength="MAX_AGENT_TEXT_BYTES"
        rows="5"
        data-voice-draft-text
      />
      <p v-if="overLimit" class="form-error" role="alert">
        文本超出长度限制（最多 {{ MAX_AGENT_TEXT_BYTES }} 字节），请精简后重试。
      </p>
      <p class="voice-disclosure" data-voice-disclosure>
        由本部署语音转写服务转写 · 提供方 {{ draft.provider }}<template v-if="disclosureExtras">{{ disclosureExtras }}</template>
      </p>
      <div class="dialog-actions">
        <button type="button" class="secondary-button" data-action="discard-draft" :disabled="submitting" @click="onDiscard">放弃</button>
        <button type="button" class="primary-button" data-action="confirm-draft" :disabled="submitting || overLimit" @click="onConfirm">{{ submitting ? '正在发送…' : '确认发送' }}</button>
      </div>
    </section>
  </div>

  <div v-if="draft && exitConfirmOpen" class="dialog-backdrop" @click.self="closeExitConfirm">
    <section
      ref="exitPanel"
      class="dialog-panel voice-exit-confirm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="voice-exit-title"
      aria-describedby="voice-exit-description"
      @keydown="onExitKeydown"
    >
      <h2 id="voice-exit-title">放弃本次转写？</h2>
      <p id="voice-exit-description">转写文本将被丢弃，需要重新录音。</p>
      <div class="dialog-actions">
        <button ref="exitCancelButton" type="button" class="secondary-button" data-action="stay-in-sheet" @click="closeExitConfirm">取消</button>
        <button type="button" class="danger-button" data-action="confirm-discard" @click="onExitConfirmDiscard">放弃</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useInjectedVoiceDraft } from '../../composables/useVoiceDraft'

/**
 * App-level draft sheet (M7b spec §4.7). No props — the draft state comes
 * from the injected `useVoiceDraft` bridge. Explicit [确认发送]/[放弃];
 * leaving via Esc (or backdrop) requires confirming "放弃本次转写？".
 */

/**
 * Mirrors `MAX_AGENT_TEXT_BYTES` from
 * `packages/protocol/src/termflow_protocol/agent.py` (64 KiB). `maxlength`
 * counts UTF-16 code units while the B side counts bytes, so the byte
 * counter below is the authoritative front-end guard for multi-byte text.
 */
const MAX_AGENT_TEXT_BYTES = 64 * 1024

const emit = defineEmits<{ closed: []; submitted: [draftId: string] }>()

const bridge = useInjectedVoiceDraft()
const panel = ref<HTMLElement | null>(null)
const exitPanel = ref<HTMLElement | null>(null)
const textEl = ref<HTMLTextAreaElement | null>(null)
const exitCancelButton = ref<HTMLButtonElement | null>(null)
const text = ref('')
const exitConfirmOpen = ref(false)
let lastPanelFocus: HTMLElement | null = null

const draft = computed(() => {
  const s = bridge?.state.value
  return s !== undefined && s.status === 'draft' ? s.draft : null
})
const submitting = computed(() => draft.value?.submitting ?? false)

// --- byte-limit guard (B side plain-text validation is byte-based) ---
function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length
}
const overLimit = computed(() => utf8Length(text.value) > MAX_AGENT_TEXT_BYTES)

// --- disclosure line (M7b spec §4.7.1) ---
const disclosureExtras = computed(() => {
  const d = draft.value
  if (d === null) return ''
  const parts: string[] = []
  if (d.region !== '') parts.push(`区域 ${d.region}`)
  if (d.language !== null && d.language !== '') parts.push(`语言 ${d.language}`)
  if (d.durationSeconds !== null) parts.push(`时长 ${Math.round(d.durationSeconds)} 秒`)
  return parts.length === 0 ? '' : ` · ${parts.join(' · ')}`
})

// --- textarea prefills with the transcript; a new draft resets it ---
watch(
  () => draft.value?.draftId,
  () => {
    if (draft.value !== null) text.value = draft.value.text
  },
  { immediate: true },
)

// --- open/close focus management ---
watch(
  () => draft.value !== null,
  (open) => {
    if (!open) {
      exitConfirmOpen.value = false
      lastPanelFocus = null
      return
    }
    void nextTick(() => textEl.value?.focus())
  },
  { immediate: true },
)

// --- actions ---
function onConfirm() {
  if (bridge === undefined || draft.value === null) return
  if (draft.value.submitFailed) bridge.retrySubmit()
  else bridge.confirm(text.value)
}

function onDiscard() {
  bridge?.discard()
}

function requestClose() {
  // Backdrop click is the same explicit exit path as Esc (§4.7.1).
  openExitConfirm()
}

function openExitConfirm() {
  lastPanelFocus = document.activeElement as HTMLElement | null
  exitConfirmOpen.value = true
  void nextTick(() => exitCancelButton.value?.focus())
}

function closeExitConfirm() {
  exitConfirmOpen.value = false
  void nextTick(() => {
    if (lastPanelFocus?.isConnected) lastPanelFocus.focus()
    else textEl.value?.focus()
  })
}

function onExitConfirmDiscard() {
  exitConfirmOpen.value = false
  onDiscard()
}

// --- keyboard: Esc opens/closes the exit confirm; Tab stays trapped ---
function onPanelKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    openExitConfirm()
    return
  }
  trapTab(event, panel.value)
}

function onExitKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeExitConfirm()
    return
  }
  trapTab(event, exitPanel.value)
}

function trapTab(event: KeyboardEvent, root: HTMLElement | null) {
  if (event.key !== 'Tab' || root === null) return
  const focusable = [...root.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled), input:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (first === undefined || last === undefined) return
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

// --- emit `closed` when the draft leaves the sheet and `submitted` on success ---
watch(
  () => bridge?.state.value.status,
  (status, previous) => {
    if (previous === 'draft' && status !== 'draft') emit('closed')
  },
)

let unsubscribeSubmitted: (() => void) | undefined
onMounted(() => {
  unsubscribeSubmitted = bridge?.onDraftSubmitted((draftId) => emit('submitted', draftId))
})
onBeforeUnmount(() => unsubscribeSubmitted?.())
</script>
