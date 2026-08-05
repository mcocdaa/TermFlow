<template>
  <div class="dialog-backdrop" @click.self="cancel">
    <section
      ref="panel"
      class="dialog-panel delete-computer-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="delete-computer-title"
      aria-describedby="delete-computer-description"
      @keydown="trapFocus"
    >
      <h2 id="delete-computer-title">删除电脑？</h2>
      <div id="delete-computer-description">
        <p>将永久删除远端注册和控制凭据：<strong>{{ computer.display_name }}</strong>。</p>
        <p>删除后，这台电脑需要重新注册才能恢复远程控制。</p>
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button
          ref="cancelButton"
          data-action="cancel-delete-computer"
          class="text-button"
          type="button"
          :disabled="pending"
          @click="cancel"
        >取消</button>
        <button
          data-action="confirm-delete-computer"
          class="danger-button"
          type="button"
          :disabled="pending"
          @click="$emit('confirm', computer.installation_id)"
        >{{ pending ? '正在删除…' : '永久删除电脑' }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { ComputerSummary } from '../../types'

const props = defineProps<{ computer: ComputerSummary; pending: boolean; error: string }>()
const emit = defineEmits<{ confirm: [installationId: string]; cancel: [] }>()
const panel = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

function cancel() {
  if (!props.pending) emit('cancel')
}

function trapFocus(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (!props.pending) {
      event.preventDefault()
      cancel()
    }
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>('button:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}

onMounted(async () => {
  restoreFocus = document.activeElement as HTMLElement | null
  await nextTick()
  cancelButton.value?.focus()
})
onBeforeUnmount(() => { if (restoreFocus?.isConnected) restoreFocus.focus() })
</script>
