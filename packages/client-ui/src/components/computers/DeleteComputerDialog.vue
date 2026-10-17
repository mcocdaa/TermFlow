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
      <h2 id="delete-computer-title">{{ t('computers.deleteDialog.title') }}</h2>
      <div id="delete-computer-description">
        <p>{{ t('computers.deleteDialog.desc1') }}<strong>{{ computer.display_name }}</strong>{{ locale === 'en' ? '.' : '。' }}</p>
        <p>{{ t('computers.deleteDialog.desc2') }}</p>
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="cleanupJobId" role="status">{{ t('dashboard.deleteDialog.cleanupPending', { jobId: cleanupJobId.slice(0, 64) }) }}</p>
      <div class="dialog-actions">
        <button
          ref="cancelButton"
          data-action="cancel-delete-computer"
          class="text-button"
          type="button"
          :disabled="pending"
          @click="cancel"
        >{{ t('common.cancel') }}</button>
        <button
          data-action="confirm-delete-computer"
          class="danger-button"
          type="button"
          :disabled="pending || Boolean(cleanupJobId)"
          @click="$emit('confirm', computer.installation_id)"
        >{{ pending ? t('dashboard.deleteDialog.deleting') : t('computers.deleteDialog.confirm') }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from '../../i18n'
import type { ComputerSummary } from '../../types'
const { t, locale } = useI18n()

const props = defineProps<{ computer: ComputerSummary; pending: boolean; error: string; cleanupJobId?: string }>()
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
