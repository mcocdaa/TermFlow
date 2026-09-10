<template>
  <div
    class="agent-composer"
    :class="{ 'agent-composer--disabled': unavailable }"
    :aria-busy="submitting ? 'true' : undefined"
    data-agent-composer
  >
    <div class="agent-composer__row">
      <div class="agent-composer__input-shell" data-agent-composer-input-shell>
        <textarea
          ref="textEl"
          v-model="text"
          class="agent-composer__input"
          rows="3"
          :maxlength="MAX_AGENT_TEXT_BYTES"
          :disabled="unavailable"
          aria-label="发送给 Agent 的消息"
          data-agent-composer-input
        />
        <button
          type="button"
          class="icon-button icon-only agent-composer__send"
          :disabled="!canSend"
          data-action="send-message"
          aria-label="发送消息"
          title="发送消息"
          @click="submit"
        >
          <Send :size="18" aria-hidden="true" />
        </button>
      </div>
      <button
        v-if="hasActiveRun"
        type="button"
        class="secondary-button agent-composer__cancel"
        :disabled="unavailable || canceling"
        :aria-busy="canceling ? 'true' : undefined"
        data-action="cancel-run"
        @click="cancelRun"
      >
        {{ canceling ? '正在取消…' : '取消运行' }}
      </button>
    </div>
    <p v-if="tooLong" class="form-error agent-composer__hint" role="alert" data-agent-composer-hint>
      文本超出长度限制（最多 {{ MAX_AGENT_TEXT_BYTES }} 字节），请精简后重试。
    </p>
    <p v-if="runtimeDown" class="agent-composer__unavailable" role="status" data-agent-composer-unavailable>
      后端运行时未就绪，暂时无法发送消息。
    </p>
  </div>
</template>

<script setup lang="ts">
//: Chat composer (M6b spec §4.7/§4.8). Client-side validation mirrors the
//: B-side ``validate_plain_text``: control characters (C0/DEL/C1) other
//: than newline are stripped before sending, the byte length is bounded by
//: ``MAX_AGENT_TEXT_BYTES`` (64 KiB, byte-based — the authoritative guard
//: for multi-byte text) and empty input cannot be sent. During submission
//: ``aria-busy`` + disabled send block double submits; a 202 clears the
//: input, emits ``submitted`` (the parent echoes the user bubble) and
//: restores focus to the textarea. Fail-closed semantics: a 503 or a
//: ``binding_runtime_unavailable`` error greys the composer out with a
//: status hint until the backend reports ``ready`` (STATE_DELTA prop
//: round-trip); other submit errors toast and keep the text for retry. The
//: cancel button appears while a run is active; a 409 ``no_active_run`` is
//: a silent UI-stale refresh, not an error.
import { computed, nextTick, ref, watch } from 'vue'
import { Send } from '@lucide/vue'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../../runtime'
import { useBottomToast } from '../../composables/useBottomToast'

const props = defineProps<{
  conversationId: string
  /** Parent-level fail-closed disable (binding revoked, conversation closed). */
  disabled?: boolean
  /** Raw backend runtime state from the history reducer (STATE_DELTA). */
  backendState?: string | null
  /**
   * An active run exists → show the cancel button. AgentChatView keeps its
   * cancel control in the detail header (single control per screen), so
   * this optional prop and the ``cancelled`` emit are currently unused
   * there; they stay for hosts that want the cancel inside the composer.
   */
  hasActiveRun?: boolean
}>()

const emit = defineEmits<{
  /** 202 accepted — the parent echoes the user bubble with ``deliveryState:'accepted'``. */
  submitted: [text: string]
  /** Cancel resolved (202 confirmed or 409 stale) — the parent refreshes run state. */
  cancelled: []
}>()

/**
 * Mirrors ``MAX_AGENT_TEXT_BYTES`` from
 * ``packages/protocol/src/termflow_protocol/agent.py`` (64 KiB). The B
 * side counts UTF-8 bytes, so the byte counter below is the authoritative
 * front-end guard for multi-byte text.
 */
const MAX_AGENT_TEXT_BYTES = 64 * 1024

const runtime = useClientRuntime()
const toast = useBottomToast()

const textEl = ref<HTMLTextAreaElement | null>(null)
const text = ref('')
const submitting = ref(false)
const canceling = ref(false)
/** Set on a 503/``binding_runtime_unavailable`` submit; lifted when the backend reports ``ready``. */
const runtimeUnavailable = ref(false)

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length
}

/**
 * Mirror the B-side ``validate_plain_text`` control-character rule (C0,
 * DEL, C1) minus the newline exception: chat input keeps line breaks and
 * silently drops every other control character.
 */
function normalizePlainText(value: string): string {
  let out = ''
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0
    if (code === 10 || !(code < 32 || (code >= 127 && code <= 159))) out += character
  }
  return out
}

function isRuntimeUnavailableError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 503 || error.code === 'binding_runtime_unavailable')
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请稍后重试。'
}

const normalizedText = computed(() => normalizePlainText(text.value))
const tooLong = computed(() => utf8ByteLength(normalizedText.value) > MAX_AGENT_TEXT_BYTES)
const runtimeDown = computed(() => props.backendState === 'unavailable' || runtimeUnavailable.value)
const unavailable = computed(() => Boolean(props.disabled) || runtimeDown.value)
const canSend = computed(() => !unavailable.value && !submitting.value && !tooLong.value && normalizedText.value.length > 0)

// The 503 fail-closed latch lifts once a STATE_DELTA reports ``ready``.
watch(
  () => props.backendState,
  (state) => {
    if (state === 'ready') runtimeUnavailable.value = false
  },
)

async function submit() {
  if (unavailable.value || submitting.value) return
  const normalized = normalizedText.value
  if (normalized.length === 0 || tooLong.value) return
  submitting.value = true
  try {
    await runtime.api.agents.submitMessage(props.conversationId, { text: normalized })
    // 202: local echo handoff + clear + keep focus (§4.8). The text is
    // only cleared on acceptance — failures retain it for retry.
    text.value = ''
    emit('submitted', normalized)
    await nextTick()
    textEl.value?.focus()
  } catch (error) {
    if (isRuntimeUnavailableError(error)) {
      runtimeUnavailable.value = true
    } else {
      toast.show({ text: errorMessage(error), tone: 'error' })
    }
  } finally {
    submitting.value = false
  }
}

async function cancelRun() {
  if (unavailable.value || canceling.value) return
  canceling.value = true
  try {
    await runtime.api.agents.cancelRun(props.conversationId)
    emit('cancelled')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // no_active_run: the UI was already stale — silent refresh, not an error.
      emit('cancelled')
    } else {
      toast.show({ text: errorMessage(error), tone: 'error' })
    }
  } finally {
    canceling.value = false
  }
}
</script>
